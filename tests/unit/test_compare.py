"""Tests for commits/compare.py on a real temporary repository.

The fixture builds its own repository (``git init``, a few commits, a branch
whose base moved on, a dirty tree with staged, unstaged and untracked files).
Never the live checkout: its state changes under the tests.
"""

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from commits import compare
from commits.git_parser import HASH_RE as gp_hash_re
from commits.compare import (
    EMPTY_TREE,
    RefError,
    commit_comparison,
    compare_commits,
    compare_detail,
    compare_diff,
    compare_file,
    compare_files,
    get_commit_detail,
    get_commit_diff,
    get_file_with_gutters,
    guess_default_base,
    list_refs,
    resolve_comparison,
    resolve_ref,
    validate_ref,
    worktree_file_path,
)


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00")
    env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"]
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env
    )
    return out.stdout.strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    """main: root (a.txt) -> second (b.txt, a.txt edited) -> third (c.txt).
    feature, branched at second: feat1 (f.txt) -> feat2 (f.txt edited).
    The checkout is on feature with a dirty tree: a.txt staged, f.txt
    unstaged, new.txt untracked, ignored.log ignored."""
    root = tmp_path_factory.mktemp("repo")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Tester")
    (root / "a.txt").write_text("alpha\nbeta\n")
    git(root, "add", "a.txt")
    git(root, "commit", "-q", "-m", "Root commit")
    (root / "a.txt").write_text("alpha\nbeta\ngamma\n")
    (root / "b.txt").write_text("b1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Second commit")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "f.txt").write_text("f1\nf2\n")
    git(root, "add", "f.txt")
    git(root, "commit", "-q", "-m", "Feature one")
    (root / "f.txt").write_text("f1\nf2 changed\nf3\n")
    git(root, "commit", "-q", "-am", "Feature two")
    git(root, "checkout", "-q", "main")
    (root / "c.txt").write_text("c1\n")
    git(root, "add", "c.txt")
    git(root, "commit", "-q", "-m", "Third commit")
    git(root, "checkout", "-q", "feature")
    # Dirty tree
    (root / "a.txt").write_text("alpha\nbeta\ngamma\nstaged\n")
    git(root, "add", "a.txt")
    (root / "f.txt").write_text("f1\nf2 changed\nf3\nunstaged\n")
    (root / "new.txt").write_text("n1\nn2\nn3\n")
    (root / ".gitignore").write_text("ignored.log\n")
    (root / "ignored.log").write_text("noise\n")
    (root / "sub").mkdir()
    (root / "sub" / "deep.txt").write_text("d1\n")
    (root / "blob.bin").write_bytes(b"\0\1\2\3")
    return root


@pytest.fixture(scope="module")
def shas(repo) -> dict:
    return {
        "root": git(repo, "rev-parse", "main~2"),
        "second": git(repo, "rev-parse", "main~1"),
        "third": git(repo, "rev-parse", "main"),
        "feat1": git(repo, "rev-parse", "feature~1"),
        "feat2": git(repo, "rev-parse", "feature"),
    }


# ---------------------------------------------------------------------------
# Ref validation and resolution
# ---------------------------------------------------------------------------


class TestValidateRef:
    @pytest.mark.parametrize(
        "ref",
        [
            "main",
            "feature/x",
            "v1.2.3",
            "HEAD~3",
            "abc123^",
            "origin/main",
            "main@{upstream}",
            "HEAD^{/fix}",
            "release-2026.09+hotfix",
        ],
    )
    def test_accepts(self, ref):
        assert validate_ref(ref) == ref

    @pytest.mark.parametrize(
        "ref",
        [
            "",
            "-p",
            "--output=/tmp/x",
            "-",
            "main..feature",
            "a b",
            "main;ls",
            "$(x)",
            "main\n",
            "..",
            "main|x",
        ],
    )
    def test_rejects(self, ref):
        with pytest.raises(RefError):
            validate_ref(ref)

    def test_rejected_refs_never_reach_git(self):
        with mock.patch("commits.git_parser._run_git") as run:
            with pytest.raises(RefError):
                resolve_ref("--output=/tmp/x", Path("/nowhere"))
        run.assert_not_called()


class TestResolveRef:
    def test_resolves_branch_and_relative(self, repo, shas):
        assert resolve_ref("main", repo) == shas["third"]
        assert resolve_ref("main~1", repo) == shas["second"]
        assert resolve_ref("feature^", repo) == shas["feat1"]
        assert resolve_ref(shas["root"][:7], repo) == shas["root"]

    def test_unknown_ref(self, repo):
        with pytest.raises(RefError):
            resolve_ref("no-such-branch", repo)

    def test_resolve_uses_end_of_options(self, repo):
        with mock.patch(
            "commits.git_parser._run_git", return_value="a" * 40 + "\n"
        ) as run:
            resolve_ref("main", repo)
        args = run.call_args[0]
        assert args[0] == "rev-parse"
        assert "--verify" in args
        assert args.index("--end-of-options") == len(args) - 2
        assert args[-1] == "main^{commit}"


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------


class TestResolveComparison:
    def test_single_commit_kind(self, repo, shas):
        cmp = resolve_comparison(repo, base="main^", head="main")
        assert cmp.kind == "commit"
        assert cmp.base_resolved == shas["second"]
        assert cmp.head_resolved == shas["third"]
        assert cmp.diff_base == shas["second"]
        assert cmp.merge_base is None

    def test_merge_base_on_a_hash_head_is_a_range(self, repo, shas):
        """The sheet always compares from the merge base. A head picked as a
        commit is a range (nothing to follow), a branch head is a branch."""
        head = git(repo, "rev-parse", "feature")
        cmp = resolve_comparison(repo, base="main", head=head, mergebase=True)
        assert cmp.kind == "range"
        assert cmp.merge_base == git(repo, "merge-base", "main", "feature")
        cmp = resolve_comparison(repo, base="main", head=head[:8], mergebase=True)
        assert cmp.kind == "range"
        cmp = resolve_comparison(repo, base="main", head="feature", mergebase=True)
        assert cmp.kind == "branch"

    def test_range_kind(self, repo, shas):
        cmp = resolve_comparison(repo, base="main~2", head="main")
        assert cmp.kind == "range"
        assert cmp.diff_base == shas["root"]

    def test_root_commit_parent_is_the_empty_tree(self, repo, shas):
        cmp = resolve_comparison(repo, base=f"{shas['root']}^", head=shas["root"])
        assert cmp.kind == "commit"
        assert cmp.base_resolved == EMPTY_TREE
        assert cmp.diff_base == EMPTY_TREE

    def test_unknown_caret_ref_is_not_the_empty_tree(self, repo):
        with pytest.raises(RefError):
            resolve_comparison(repo, base="nope^", head="main")

    def test_branch_with_merge_base(self, repo, shas):
        cmp = resolve_comparison(repo, base="main", head="feature", mergebase=True)
        assert cmp.kind == "branch"
        assert cmp.base_resolved == shas["third"]  # what "main" points at
        assert cmp.merge_base == shas["second"]  # where feature forked
        assert cmp.diff_base == shas["second"]
        assert cmp.head_resolved == shas["feat2"]
        assert cmp.mergebase is True
        assert cmp.base == "main" and cmp.head == "feature"  # kept as typed

    def test_two_dot_branch_without_merge_base(self, repo, shas):
        cmp = resolve_comparison(repo, base="main", head="feature")
        assert cmp.kind == "range"
        assert cmp.diff_base == shas["third"]

    def test_worktree(self, repo, shas):
        cmp = resolve_comparison(repo, worktree=True)
        assert cmp.kind == "worktree"
        assert cmp.base == "HEAD"
        assert cmp.base_resolved == shas["feat2"]
        assert cmp.head_resolved is None
        assert cmp.head == ""

    def test_missing_refs(self, repo):
        with pytest.raises(RefError):
            resolve_comparison(repo, head="main")
        with pytest.raises(RefError):
            resolve_comparison(repo, base="main")

    def test_to_dict_has_short_hashes(self, repo, shas):
        d = resolve_comparison(repo, base="main^", head="main").to_dict()
        assert d["head_short"] == shas["third"][:7]
        assert d["base_short"] == shas["second"][:7]
        assert d["diff_base_short"] == shas["second"][:7]


class TestCompareFiles:
    def test_single_commit_matches_old_payload(self, repo, shas):
        detail = get_commit_detail(shas["second"], repo)
        assert detail["hash"] == shas["second"]
        assert detail["short"] == shas["second"][:7]
        assert detail["author"] == "Tester"
        assert detail["message"] == "Second commit"
        assert detail["body"] == ""
        assert detail["files"] == [
            {"path": "a.txt", "status": "M", "insertions": 1, "deletions": 0},
            {"path": "b.txt", "status": "A", "insertions": 1, "deletions": 0},
        ]

    def test_root_commit_against_empty_tree(self, repo, shas):
        detail = get_commit_detail(shas["root"], repo)
        assert detail["files"] == [
            {"path": "a.txt", "status": "A", "insertions": 2, "deletions": 0}
        ]
        diff = get_commit_diff(shas["root"], repo)
        assert [f["path"] for f in diff["files"]] == ["a.txt"]
        assert diff["files"][0]["status"] == "A"
        adds = [
            line["content"]
            for line in diff["files"][0]["hunks"][0]["lines"]
            if line["type"] == "add"
        ]
        assert adds == ["alpha", "beta"]

    def test_range(self, repo, shas):
        cmp = resolve_comparison(repo, base="main~2", head="main")
        files = compare_files(cmp, repo)
        assert {f["path"]: f["status"] for f in files} == {
            "a.txt": "M",
            "b.txt": "A",
            "c.txt": "A",
        }
        commits, count, oldest = compare_commits(cmp, repo)
        assert count == 2
        assert [c["message"] for c in commits] == ["Third commit", "Second commit"]
        assert oldest == commits[-1]

    def test_branch_against_merge_base_excludes_base_advance(self, repo, shas):
        cmp = resolve_comparison(repo, base="main", head="feature", mergebase=True)
        files = compare_files(cmp, repo)
        assert [f["path"] for f in files] == ["f.txt"]
        commits, count, oldest = compare_commits(cmp, repo)
        assert count == 2
        assert [c["message"] for c in commits] == ["Feature two", "Feature one"]
        assert oldest is not None and oldest["message"] == "Feature one"
        detail = compare_detail(cmp, repo)
        assert detail["kind"] == "branch"
        assert detail["commit_count"] == 2
        assert len(detail["commits"]) == 2
        assert detail["oldest"]["hash"] == shas["feat1"]
        assert detail["files"][0]["path"] == "f.txt"
        # The base's own hash and the merge base are distinct and both reported
        assert detail["base_short"] == shas["third"][:7]
        assert detail["merge_base_short"] == shas["second"][:7]
        assert detail["diff_base_short"] == detail["merge_base_short"]

    def test_oldest_commit_survives_the_list_cap(self, repo, shas):
        cmp = resolve_comparison(repo, base=f"{shas['root']}^", head="main")
        with mock.patch("commits.compare.MAX_COMMITS", 2):
            commits, count, oldest = compare_commits(cmp, repo)
            detail = compare_detail(cmp, repo)
        assert count == 3 and len(commits) == 2
        assert commits[-1]["hash"] == shas["second"]
        assert oldest is not None and oldest["hash"] == shas["root"]
        assert oldest["message"] == "Root commit"
        assert detail["oldest"]["hash"] == shas["root"]
        assert detail["commit_count"] == 3

    def test_worktree_staged_unstaged_untracked(self, repo):
        cmp = resolve_comparison(repo, worktree=True)
        files = {f["path"]: f for f in compare_files(cmp, repo)}
        assert files["a.txt"]["status"] == "M"  # staged
        assert files["a.txt"]["insertions"] == 1
        assert files["f.txt"]["status"] == "M"  # unstaged
        assert files["new.txt"] == {
            "path": "new.txt",
            "status": "A",
            "insertions": 3,
            "deletions": 0,
        }
        assert files["sub/deep.txt"]["status"] == "A"
        assert files["blob.bin"]["insertions"] == 0
        assert "ignored.log" not in files
        assert ".gitignore" in files
        commits, count, oldest = compare_commits(cmp, repo)
        assert commits == [] and count == 0 and oldest is None

    def test_worktree_diff_includes_untracked_as_added(self, repo):
        cmp = resolve_comparison(repo, worktree=True)
        diff = compare_diff(cmp, repo)
        by_path = {f["path"]: f for f in diff["files"]}
        assert by_path["new.txt"]["status"] == "A"
        adds = [
            line["content"]
            for line in by_path["new.txt"]["hunks"][0]["lines"]
            if line["type"] == "add"
        ]
        assert adds == ["n1", "n2", "n3"]
        assert by_path["a.txt"]["status"] == "M"
        assert by_path["blob.bin"].get("binary") is True

    def test_root_commit_diff_in_a_range(self, repo, shas):
        cmp = resolve_comparison(repo, base=f"{shas['root']}^", head="main")
        assert cmp.diff_base == EMPTY_TREE
        commits, count, oldest = compare_commits(cmp, repo)
        assert count == 3
        assert oldest is not None and oldest["hash"] == shas["root"]


class TestCompareFile:
    def test_commit_file_with_gutters(self, repo, shas):
        data = get_file_with_gutters(shas["second"], "a.txt", repo)
        assert data["content"] == "alpha\nbeta\ngamma\n"
        assert [line["gutter"] for line in data["lines"]] == [None, None, "added"]

    def test_commit_file_not_found(self, repo, shas):
        with pytest.raises(FileNotFoundError):
            get_file_with_gutters(shas["second"], "f.txt", repo)

    def test_commit_file_that_is_a_directory_is_not_found(self, repo, shas):
        git(repo, "add", "sub/deep.txt")
        git(repo, "commit", "-q", "-m", "Add sub/deep.txt")
        try:
            head = git(repo, "rev-parse", "HEAD")
            data = get_file_with_gutters(head, "sub/deep.txt", repo)
            assert data["content"] == "d1\n"
            with pytest.raises(FileNotFoundError):
                get_file_with_gutters(head, "sub", repo)
        finally:
            git(repo, "reset", "-q", "--soft", "HEAD~1")
            git(repo, "reset", "-q", "sub/deep.txt")

    def test_branch_file_gutters_against_merge_base(self, repo):
        cmp = resolve_comparison(repo, base="main", head="feature", mergebase=True)
        data = compare_file(cmp, "f.txt", repo)
        assert data["content"] == "f1\nf2 changed\nf3\n"
        assert [line["gutter"] for line in data["lines"]] == [
            "added",
            "added",
            "added",
        ]

    def test_worktree_tracked_file_from_disk(self, repo):
        cmp = resolve_comparison(repo, worktree=True)
        data = compare_file(cmp, "f.txt", repo)
        assert data["content"].endswith("unstaged\n")
        assert data["lines"][-1]["gutter"] == "added"

    def test_worktree_untracked_file_all_added(self, repo):
        cmp = resolve_comparison(repo, worktree=True)
        data = compare_file(cmp, "new.txt", repo)
        assert [line["gutter"] for line in data["lines"]] == ["added"] * 3

    def test_worktree_missing_file(self, repo):
        cmp = resolve_comparison(repo, worktree=True)
        with pytest.raises(FileNotFoundError):
            compare_file(cmp, "missing.txt", repo)
        with pytest.raises(FileNotFoundError):
            compare_file(cmp, "sub", repo)

    def test_worktree_symlink_escaping_root_refused(self, repo, tmp_path):
        outside = tmp_path / "secret.txt"
        outside.write_text("secret\n")
        link = repo / "escape.txt"
        link.symlink_to(outside)
        try:
            cmp = resolve_comparison(repo, worktree=True)
            with pytest.raises(ValueError):
                compare_file(cmp, "escape.txt", repo)
            with pytest.raises(ValueError):
                worktree_file_path("escape.txt", repo)
        finally:
            link.unlink()

    def test_untracked_escaping_symlink_is_listed_but_never_read(self, repo, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("s1\ns2\ns3\n")
        link = repo / "escape.txt"
        link.symlink_to(outside)
        try:
            cmp = resolve_comparison(repo, worktree=True)
            files = {f["path"]: f for f in compare_files(cmp, repo)}
            assert files["escape.txt"] == {
                "path": "escape.txt",
                "status": "A",
                "insertions": 0,
                "deletions": 0,
            }
            detail = compare_detail(cmp, repo)
            assert "escape.txt" in {f["path"] for f in detail["files"]}
            diff = {f["path"]: f for f in compare_diff(cmp, repo)["files"]}
            assert diff["escape.txt"]["status"] == "A"
            assert diff["escape.txt"]["hunks"] == []
            with pytest.raises(ValueError):
                compare_file(cmp, "escape.txt", repo)
        finally:
            link.unlink()

    def test_untracked_fifo_is_listed_but_never_opened(self, repo):
        fifo = repo / "pipe.fifo"
        os.mkfifo(fifo)
        try:
            cmp = resolve_comparison(repo, worktree=True)
            # git ls-files --others skips non-regular files, so it is not
            # listed at all, and a direct read is refused without opening it
            files = {f["path"]: f for f in compare_files(cmp, repo)}
            assert "pipe.fifo" not in files
            diff = {f["path"]: f for f in compare_diff(cmp, repo)["files"]}
            assert "pipe.fifo" not in diff
            with pytest.raises(FileNotFoundError):
                compare_file(cmp, "pipe.fifo", repo)
        finally:
            fifo.unlink()

    def test_worktree_symlink_inside_root_allowed(self, repo):
        link = repo / "inside.txt"
        link.symlink_to(repo / "new.txt")
        try:
            assert worktree_file_path("inside.txt", repo) == (repo / "new.txt")
        finally:
            link.unlink()


# ---------------------------------------------------------------------------
# Every git call built from user input carries --end-of-options and --
# ---------------------------------------------------------------------------


class TestGitCallSafety:
    def test_every_ref_bearing_call_is_guarded(self, repo):
        calls = []
        real = compare.gp._run_git

        def spy(*args, repo_dir, check=True):
            calls.append(args)
            return real(*args, repo_dir=repo_dir, check=check)

        with mock.patch("commits.git_parser._run_git", side_effect=spy):
            cmp = resolve_comparison(repo, base="main", head="feature", mergebase=True)
            compare_detail(cmp, repo)
            compare_diff(cmp, repo)
            compare_file(cmp, "f.txt", repo)
            wt = resolve_comparison(repo, worktree=True)
            compare_diff(wt, repo)
            compare_file(wt, "f.txt", repo)
            compare_file(wt, "new.txt", repo)
        assert not any(a[0] == "show" and ":" in a[-1] for a in calls)
        for args in calls:
            if args[0] in (
                "rev-parse",
                "merge-base",
                "rev-list",
                "log",
                "ls-tree",
                "cat-file",
            ):
                assert "--end-of-options" in args, args
            if args[0] == "diff" and "--no-index" not in args:
                assert "--end-of-options" in args, args
                assert "--" in args, args
            if args[0] == "diff" and "--no-index" in args:
                assert args.index("--") < args.index("/dev/null"), args
            if args[0] == "ls-files" and "--others" not in args:
                assert "--" in args, args  # the tracked check carries a path
            if args[0] == "cat-file":
                # blob --end-of-options <40-hex oid>, the boundary before the id
                assert args[1] == "blob", args
                assert args.index("--end-of-options") == len(args) - 2, args
                assert len(args[-1]) == 40 and gp_hash_re.match(args[-1]), args
            # The user's path only ever appears after "--", whole, never
            # embedded in a revision argument like "<sha>:<path>".
            for i, a in enumerate(args):
                if "f.txt" in a or "new.txt" in a:
                    assert a in ("f.txt", "new.txt"), args
                    assert "--" in args and args.index("--") < i, args


# ---------------------------------------------------------------------------
# Refs
# ---------------------------------------------------------------------------


class TestRefs:
    def test_list_refs(self, repo):
        refs = list_refs(repo)
        assert refs["current"] == "feature"
        assert set(refs["local"]) == {"main", "feature"}
        assert refs["remote"] == []
        assert refs["default_base"] == "main"
        # The recent commits, newest first, for the sheet's pickers
        commits = refs["commits"]
        assert [c["message"] for c in commits] == [
            "Feature two",
            "Feature one",
            "Second commit",
            "Root commit",
        ]
        assert commits[0]["hash"] == git(repo, "rev-parse", "feature")
        assert set(commits[0]) == {"hash", "short", "author", "date", "message"}

    def test_guess_default_base(self):
        assert (
            guess_default_base(["main", "feature"], ["origin/dev"], "feature", None)
            == "main"
        )
        assert guess_default_base(["master", "x"], [], "x", None) == "master"
        assert guess_default_base(["x", "y"], [], "x", None) == "y"
        assert guess_default_base(["x"], [], "x", None) is None
        # origin/HEAD wins, as the local branch of that name when it exists
        assert (
            guess_default_base(["dev", "feat"], ["origin/dev"], "feat", "origin/dev")
            == "dev"
        )
        assert (
            guess_default_base(["feat"], ["origin/dev"], "feat", "origin/dev")
            == "origin/dev"
        )
        # never the current branch
        assert guess_default_base(["main", "x"], [], "main", None) == "x"

    def test_detached_head_has_no_current(self, repo, shas):
        git(repo, "checkout", "-q", "--detach", shas["feat2"])
        try:
            assert list_refs(repo)["current"] is None
            cmp = resolve_comparison(repo, worktree=True)
            assert cmp.base_resolved == shas["feat2"]
        finally:
            git(repo, "checkout", "-q", "feature")


class TestCommitComparison:
    def test_unknown_hash_is_a_ref_error(self, repo):
        with pytest.raises(RefError):
            commit_comparison("deadbeef", repo)

    def test_invalid_hash_is_a_value_error(self, repo):
        with pytest.raises(ValueError):
            commit_comparison("not-a-hash", repo)
