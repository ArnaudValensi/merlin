"""Tests for commits/reviews.py: the store, the lock, the viewed rule, the
head-moved count. On a temporary repository and a temporary MERLIN_HOME
(the shared conftest points it at tmp_path), never the real home."""

import json
import multiprocessing
import os
import subprocess
from pathlib import Path

import pytest

import paths
from commits import reviews as rv
from commits.compare import RefError, resolve_comparison


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00")
    env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"]
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """main: root -> second. feature from second: feat1. main then moves on
    (third). Checked out on feature with an untracked file."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Tester")
    (root / "a.txt").write_text("alpha\n")
    git(root, "add", "a.txt")
    git(root, "commit", "-q", "-m", "Root commit")
    (root / "a.txt").write_text("alpha\nbeta\n")
    git(root, "commit", "-q", "-am", "Second commit")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "f.txt").write_text("f1\n")
    git(root, "add", "f.txt")
    git(root, "commit", "-q", "-m", "Feature one")
    git(root, "checkout", "-q", "main")
    (root / "c.txt").write_text("c\n")
    git(root, "add", "c.txt")
    git(root, "commit", "-q", "-m", "Third commit")
    git(root, "checkout", "-q", "feature")
    (root / "new.txt").write_text("n1\n")
    return root


def branch_review(repo) -> dict:
    cmp = resolve_comparison(repo, base="main", head="feature", mergebase=True)
    return rv.create(repo, cmp)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStore:
    def test_reviews_dir_under_data_dir(self, tmp_path):
        assert paths.reviews_dir() == tmp_path / "reviews"
        assert rv.reviews_dir() == paths.reviews_dir()

    def test_create_writes_the_record_shape(self, repo):
        review = branch_review(repo)
        assert rv.ID_RE.fullmatch(review["id"])
        path = rv.reviews_dir() / f"{review['id']}.json"
        assert path.exists()
        stored = json.loads(path.read_text())
        assert stored == review
        assert set(review) == {
            "id",
            "repo",
            "title",
            "kind",
            "base",
            "head",
            "mergebase",
            "base_resolved",
            "head_resolved",
            "last_seen_head",
            "status",
            "created",
            "updated",
            "files",
            "comments",
        }
        assert review["repo"] == str(repo.resolve())
        assert review["title"] == "feature vs main"
        assert review["kind"] == "branch"
        assert review["base"] == "main" and review["head"] == "feature"
        assert review["mergebase"] is True
        assert review["base_resolved"] == git(repo, "rev-parse", "main")
        assert review["head_resolved"] == git(repo, "rev-parse", "feature")
        assert review["last_seen_head"] == review["head_resolved"]
        assert review["status"] == "open"
        assert review["files"] == {} and review["comments"] == []
        # No temp file left behind, and the lock file exists
        assert not list(rv.reviews_dir().glob("*.tmp"))
        assert (rv.reviews_dir() / f"{review['id']}.lock").exists()

    def test_default_titles(self, repo):
        wt = rv.create(repo, resolve_comparison(repo, worktree=True))
        assert wt["title"] == "Working tree" and wt["kind"] == "worktree"
        assert wt["head_resolved"] is None and wt["last_seen_head"] is None
        one = rv.create(repo, resolve_comparison(repo, base="main^", head="main"))
        assert one["title"] == "Third commit" and one["kind"] == "commit"
        root = git(repo, "rev-parse", "main~2")
        rng = rv.create(repo, resolve_comparison(repo, base=f"{root}^", head="main"))
        assert (
            rng["title"]
            == f"{root[:7]}..{git(repo, 'rev-parse', '--short=7', 'main')} (3 commits)"
        )
        titled = rv.create(
            repo, resolve_comparison(repo, worktree=True), "  my   review "
        )
        assert titled["title"] == "my review"

    def test_load_and_unknown(self, repo):
        review = branch_review(repo)
        assert rv.load(review["id"]) == review
        with pytest.raises(rv.ReviewNotFound):
            rv.load("00000000")
        with pytest.raises(ValueError):
            rv.load("not-an-id")
        with pytest.raises(ValueError):
            rv.load("../../etc")

    def test_list_for_repo(self, repo, tmp_path):
        a = branch_review(repo)
        b = rv.create(repo, resolve_comparison(repo, worktree=True))
        other = tmp_path / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        git(other, "config", "user.email", "t@example.com")
        git(other, "config", "user.name", "Tester")
        (other / "x").write_text("x\n")
        git(other, "add", "x")
        git(other, "commit", "-q", "-m", "x")
        rv.create(other, resolve_comparison(other, worktree=True))
        listed = rv.list_for_repo(str(repo.resolve()))
        assert {r["id"] for r in listed} == {a["id"], b["id"]}
        assert listed[0]["id"] == b["id"]  # newest update first
        assert set(listed[0]) >= {
            "id",
            "title",
            "kind",
            "status",
            "updated",
            "viewed_count",
            "open_comments",
        }
        assert rv.list_for_repo("/nowhere") == []

    def test_malformed_file_is_reported_never_replaced(self, repo):
        review = branch_review(repo)
        path = rv.reviews_dir() / f"{review['id']}.json"
        path.write_text("{not json")
        with pytest.raises(rv.ReviewCorrupt):
            rv.load(review["id"])
        with pytest.raises(rv.ReviewCorrupt):
            rv.set_title(review["id"], "x")
        with pytest.raises(rv.ReviewCorrupt):
            rv.refresh(review["id"], repo)
        assert path.read_text() == "{not json"
        listed = rv.list_for_repo(str(repo.resolve()))
        assert listed and listed[0]["id"] == review["id"] and "error" in listed[0]
        # A JSON object for another id is malformed too
        path.write_text(json.dumps({"id": "deadbeef"}))
        with pytest.raises(rv.ReviewCorrupt):
            rv.load(review["id"])

    def test_title_and_status(self, repo):
        review = branch_review(repo)
        before = review["updated"]
        updated = rv.set_title(review["id"], "  Better   title ")
        assert updated["title"] == "Better title"
        assert updated["updated"] >= before
        with pytest.raises(ValueError):
            rv.set_title(review["id"], "   ")
        assert rv.set_status(review["id"], "closed")["status"] == "closed"
        assert rv.set_status(review["id"], "open")["status"] == "open"
        with pytest.raises(ValueError):
            rv.set_status(review["id"], "done")
        assert rv.load(review["id"])["title"] == "Better title"

    def test_atomic_write_leaves_no_partial_file(self, repo, monkeypatch):
        review = branch_review(repo)
        path = rv.reviews_dir() / f"{review['id']}.json"
        original = path.read_text()
        real_replace = os.replace

        def failing_replace(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(rv.os, "replace", failing_replace)
        with pytest.raises(OSError):
            rv.set_title(review["id"], "lost")
        monkeypatch.setattr(rv.os, "replace", real_replace)
        assert path.read_text() == original
        assert rv.load(review["id"])["title"] == "feature vs main"


def _bump(args):
    """Worker for the lock test: N read-modify-writes of the same review."""
    home, review_id, key, n = args
    os.environ["MERLIN_HOME"] = home
    from commits import reviews as store

    for i in range(n):

        def fn(review, key=key, i=i):
            review["files"][f"{key}-{i}"] = {"viewed_at": "x", "viewed_hash": "y"}

        store.update(review_id, fn)
    return key


class TestLock:
    def test_two_processes_keep_both_changes(self, repo, tmp_path):
        review = branch_review(repo)
        home = os.environ["MERLIN_HOME"]
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(2) as pool:
            pool.map(
                _bump, [(home, review["id"], "a", 25), (home, review["id"], "b", 25)]
            )
        files = rv.load(review["id"])["files"]
        assert len(files) == 50
        assert {k for k in files if k.startswith("a-")} == {f"a-{i}" for i in range(25)}
        assert {k for k in files if k.startswith("b-")} == {f"b-{i}" for i in range(25)}


# ---------------------------------------------------------------------------
# Viewed files and the head-moved count
# ---------------------------------------------------------------------------


class TestViewed:
    def test_tick_stores_the_patch_hash(self, repo):
        review = branch_review(repo)
        ticked = rv.set_viewed(review["id"], "f.txt", True, repo)
        entry = ticked["files"]["f.txt"]
        assert set(entry) == {"viewed_at", "viewed_hash"}
        cmp = resolve_comparison(repo, base="main", head="feature", mergebase=True)
        assert entry["viewed_hash"] == rv.patch_hash(cmp, "f.txt", repo)
        assert len(entry["viewed_hash"]) == 40
        unticked = rv.set_viewed(review["id"], "f.txt", False, repo)
        assert unticked["files"] == {}

    def test_hash_is_over_the_file_patch_not_the_whole_diff(self, repo):
        cmp = resolve_comparison(repo, worktree=True)
        (repo / "a.txt").write_text("alpha\nbeta\nchanged\n")
        h_new = rv.patch_hash(cmp, "new.txt", repo)
        h_a = rv.patch_hash(cmp, "a.txt", repo)
        assert h_new != h_a
        (repo / "a.txt").write_text("alpha\nbeta\nchanged again\n")
        assert rv.patch_hash(cmp, "new.txt", repo) == h_new  # untouched file
        assert rv.patch_hash(cmp, "a.txt", repo) != h_a
        git(repo, "checkout", "-q", "--", "a.txt")

    def test_unchanged_keeps_the_tick_changed_clears_and_reports(self, repo):
        review = branch_review(repo)
        rv.set_viewed(review["id"], "f.txt", True, repo)
        loaded, changed, new_commits, cmp = rv.refresh(review["id"], repo)
        assert "f.txt" in loaded["files"] and changed == [] and new_commits == 0
        # Amend the file on the branch: the patch changes, the tick goes
        (repo / "f.txt").write_text("f1\nf2\n")
        git(repo, "commit", "-q", "-am", "Feature two")
        loaded, changed, new_commits, cmp = rv.refresh(review["id"], repo)
        assert changed == ["f.txt"]
        assert loaded["files"] == {}
        assert new_commits == 1
        assert loaded["last_seen_head"] == git(repo, "rev-parse", "feature")
        assert rv.load(review["id"])["files"] == {}
        # The next look reports nothing new
        loaded, changed, new_commits, cmp = rv.refresh(review["id"], repo)
        assert changed == [] and new_commits == 0

    def test_base_advance_keeps_the_tick_in_merge_base_mode(self, repo):
        review = branch_review(repo)
        rv.set_viewed(review["id"], "f.txt", True, repo)
        git(repo, "checkout", "-q", "main")
        (repo / "c.txt").write_text("c2\n")
        git(repo, "commit", "-q", "-am", "Main moves again")
        git(repo, "checkout", "-q", "feature")
        loaded, changed, new_commits, cmp = rv.refresh(review["id"], repo)
        assert changed == [] and new_commits == 0
        assert "f.txt" in loaded["files"]
        assert cmp.base_resolved == git(repo, "rev-parse", "main")  # followed
        assert cmp.merge_base == git(repo, "rev-parse", "main~2")

    def test_worktree_tick_clears_when_the_file_changes_on_disk(self, repo):
        review = rv.create(repo, resolve_comparison(repo, worktree=True))
        rv.set_viewed(review["id"], "new.txt", True, repo)
        loaded, changed, _, _ = rv.refresh(review["id"], repo)
        assert "new.txt" in loaded["files"] and changed == []
        (repo / "new.txt").write_text("n1\nn2\n")
        loaded, changed, new_commits, _ = rv.refresh(review["id"], repo)
        assert changed == ["new.txt"] and loaded["files"] == {}
        assert new_commits == 0 and loaded["last_seen_head"] is None

    def test_refresh_writes_only_when_something_changed(self, repo):
        review = branch_review(repo)
        path = rv.reviews_dir() / f"{review['id']}.json"
        before = path.stat().st_mtime_ns
        rv.refresh(review["id"], repo)
        assert path.stat().st_mtime_ns == before

    def test_new_commits_counted_before_last_seen_advances(self, repo):
        review = branch_review(repo)
        (repo / "f.txt").write_text("f1\nf2\n")
        git(repo, "commit", "-q", "-am", "Feature two")
        (repo / "f.txt").write_text("f1\nf2\nf3\n")
        git(repo, "commit", "-q", "-am", "Feature three")
        loaded, changed, new_commits, _ = rv.refresh(review["id"], repo)
        assert new_commits == 2
        assert loaded["last_seen_head"] == git(repo, "rev-parse", "feature")
        assert rv.refresh(review["id"], repo)[2] == 0

    def test_deleted_branch_is_a_ref_error(self, repo):
        review = branch_review(repo)
        git(repo, "checkout", "-q", "main")
        git(repo, "branch", "-q", "-D", "feature")
        with pytest.raises(RefError):
            rv.refresh(review["id"], repo)
        assert rv.load(review["id"])["head"] == "feature"  # untouched

    def test_viewed_path_is_validated(self, repo):
        review = branch_review(repo)
        with pytest.raises(ValueError):
            rv.set_viewed(review["id"], "../x", True, repo)
