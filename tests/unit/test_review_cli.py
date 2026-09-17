"""Tests for ``merlin review`` (commits/review_cli.py) on a fixture review in
a temporary repository under the isolated MERLIN_HOME."""

import io
import json
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from commits import reviews as rv
from commits.compare import resolve_comparison
from commits.review_cli import main


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00")
    env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"]
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Tester")
    (root / "a.txt").write_text("alpha\n")
    git(root, "add", "a.txt")
    git(root, "commit", "-q", "-m", "Root commit")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "f.txt").write_text("f1\nf2\n")
    git(root, "add", "f.txt")
    git(root, "commit", "-q", "-m", "Feature one")
    return root


@pytest.fixture
def review(repo) -> dict:
    cmp = resolve_comparison(repo, base="main", head="feature", mergebase=True)
    r = rv.create(repo, cmp)
    rv.set_viewed(r["id"], "f.txt", True, repo)
    _, c = rv.add_comment(
        r["id"], repo, body="Why f2?", path="f.txt", side="new", line=2
    )
    rv.add_comment(r["id"], repo, body="Looks fine overall")
    r = rv.load(r["id"])
    r["_thread"] = c["id"]
    return r


def run(*argv, cwd=None):
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    if cwd:
        os.chdir(cwd)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()


class TestList:
    def test_lists_open_reviews_of_the_cwd_repo(self, repo, review):
        code, out, err = run("list", cwd=repo)
        assert code == 0, err
        assert review["id"] in out
        assert "feature vs main" in out
        assert "2 open threads" in out

    def test_all_includes_closed_and_json_roundtrips(self, repo, review):
        rv.set_status(review["id"], "closed")
        code, out, _ = run("list", "--repo", str(repo))
        assert review["id"] not in out and "No reviews" in out
        code, out, _ = run("list", "--all", "--repo", str(repo), "--json")
        data = json.loads(out)
        assert data[0]["id"] == review["id"] and data[0]["status"] == "closed"

    def test_not_a_repository(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        code, out, err = run("list", "--repo", str(plain))
        assert code == 1 and "Not a git repository" in err


class TestShow:
    def test_markdown(self, repo, review):
        code, out, err = run("show", review["id"])
        assert code == 0, err
        assert out.startswith(f"# feature vs main ({review['id']})")
        assert "Kind: branch" in out and "Status: open" in out
        assert "main (" in out and ".. feature (" in out and "merge base" in out
        assert "1 commit" in out
        assert "## Files (1 of 1 viewed)" in out
        assert "- [x] f.txt (A, +2 -0) · 1 open thread" in out
        assert "## Open threads (2)" in out
        assert f"### {review['_thread']} · f.txt:2 (new) · user" in out
        assert "> f2" in out
        assert "Why f2?" in out
        assert "review-wide" in out and "Looks fine overall" in out
        assert "merlin review reply" in out

    def test_resolved_threads_counted_unless_all(self, repo, review):
        rv.resolve(review["id"], review["_thread"], "agent", "Done")
        code, out, _ = run("show", review["id"])
        assert "## Open threads (1)" in out
        assert "Resolved threads: 1 (use --all to include them)" in out
        assert "Why f2?" not in out
        code, out, _ = run("show", review["id"], "--all")
        assert "## Resolved threads (1)" in out
        assert "[resolved by agent]" in out and "Done" in out

    def test_moved_and_outdated_labels_and_new_commits(self, repo, review):
        (repo / "f.txt").write_text("f0\nf1\nf2\n")
        git(repo, "commit", "-q", "-am", "Shift")
        code, out, _ = run("show", review["id"])
        assert "[moved]" in out
        assert "1 new commit on the head since the user last looked." in out
        # The agent's look does not consume the user's note
        assert rv.load(review["id"])["last_seen_head"] == review["last_seen_head"]
        (repo / "f.txt").write_text("x\ny\nz\n")
        git(repo, "commit", "-q", "-am", "Rewrite")
        code, out, _ = run("show", review["id"])
        assert "[outdated]" in out and "> f2" in out
        assert "2 new commits on the head" in out

    def test_json(self, repo, review):
        code, out, _ = run("show", review["id"], "--json")
        data = json.loads(out)
        assert data["review"]["id"] == review["id"]
        assert data["comparison"]["kind"] == "branch"
        assert data["anchors"] == {review["_thread"]: "current"}
        assert data["open_threads"] == {"f.txt": 1, "": 1}

    def test_unknown_review(self, repo):
        code, out, err = run("show", "00000000")
        assert code == 1 and "Unknown review" in err
        code, out, err = run("show", "nope")
        assert code == 1 and "Invalid review id" in err

    def test_deleted_branch_reports_the_error(self, repo, review):
        git(repo, "checkout", "-q", "main")
        git(repo, "branch", "-q", "-D", "feature")
        code, out, err = run("show", review["id"])
        assert code == 0
        assert "Error: Unknown ref: feature" in out
        assert "## Open threads (2)" in out  # the threads are still printed


class TestDiff:
    def test_unified_diff_as_git_prints_it(self, repo, review):
        code, out, _ = run("diff", review["id"])
        assert code == 0
        assert out.startswith("diff --git a/f.txt b/f.txt")
        assert "+f1\n+f2\n" in out
        code, out, _ = run("diff", review["id"], "--path", "a.txt")
        assert out == ""
        code, out, err = run("diff", review["id"], "--path", "../x")
        assert code == 1

    def test_worktree_diff_includes_untracked(self, repo):
        (repo / "new.txt").write_text("n1\n")
        r = rv.create(repo, resolve_comparison(repo, worktree=True))
        code, out, _ = run("diff", r["id"])
        assert "diff --git a/new.txt b/new.txt" in out and "+n1" in out


class TestMutations:
    def test_comment_reply_resolve_reopen(self, repo, review):
        code, out, err = run(
            "comment", review["id"], "--path", "f.txt", "--line", "1", "Rename", "f1"
        )
        assert code == 0, err
        cid = out.split()[1]
        stored = {c["id"]: c for c in rv.load(review["id"])["comments"]}
        assert stored[cid]["body"] == "Rename f1"
        assert stored[cid]["author"] == "agent" and stored[cid]["line_text"] == "f1"
        code, out, err = run(
            "reply", review["id"], cid, "--author", "user", "Yes please"
        )
        assert code == 0 and "Replied" in out
        code, out, err = run(
            "resolve", review["id"], cid, "-m", "Renamed in the next commit"
        )
        assert code == 0 and "Resolved" in out
        c = {c["id"]: c for c in rv.load(review["id"])["comments"]}[cid]
        assert c["status"] == "resolved" and c["resolved_by"] == "agent"
        assert [r["author"] for r in c["replies"]] == ["user", "agent"]
        code, out, err = run("reopen", review["id"], cid)
        assert code == 0
        assert {c["id"]: c for c in rv.load(review["id"])["comments"]}[cid][
            "status"
        ] == "open"

    def test_comment_on_a_line_that_does_not_exist_fails(self, repo, review):
        before = rv.load(review["id"])
        code, out, err = run(
            "comment", review["id"], "--path", "f.txt", "--line", "9", "x"
        )
        assert code == 1 and "has 2 lines" in err
        code, out, err = run("comment", review["id"], "--path", "f.txt", "x")
        assert code == 1 and "both --path and --line" in err
        code, out, err = run(
            "comment",
            review["id"],
            "--path",
            "f.txt",
            "--line",
            "1",
            "--side",
            "old",
            "x",
        )
        assert code == 1 and "no old side" in err
        assert rv.load(review["id"])["comments"] == before["comments"]

    def test_review_wide_comment_and_stdin(self, repo, review, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("From stdin\nsecond line\n"))
        code, out, err = run("comment", review["id"], "-")
        assert code == 0, err
        last = rv.load(review["id"])["comments"][-1]
        assert last["path"] is None and last["body"] == "From stdin\nsecond line"

    def test_unknown_thread(self, repo, review):
        code, out, err = run("reply", review["id"], "deadbeef", "x")
        assert code == 1 and "Unknown comment" in err
        code, out, err = run("resolve", review["id"], "deadbeef")
        assert code == 1

    def test_close_and_reopen_review(self, repo, review):
        code, out, _ = run("close", review["id"])
        assert code == 0 and rv.load(review["id"])["status"] == "closed"
        code, out, _ = run("reopen-review", review["id"])
        assert code == 0 and rv.load(review["id"])["status"] == "open"


class TestRegistration:
    def test_review_is_a_delegated_core_command(self):
        import ext_commands
        from cli import DELEGATED_COMMANDS

        assert "review" in DELEGATED_COMMANDS
        assert "review" in ext_commands.CORE_COMMANDS

    def test_help(self):
        with pytest.raises(SystemExit) as exc:
            run("--help")
        assert exc.value.code == 0
