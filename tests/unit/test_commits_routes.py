"""Tests for commits/routes.py — API endpoint tests."""

from unittest import mock

import pytest

# We need to disable auth for testing
import main as app_mod


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    """Disable auth for all route tests."""
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


@pytest.fixture(autouse=True)
def _mock_repo_root(monkeypatch):
    """Mock _find_repo_root to return a fake path so _resolve_repo works."""
    from pathlib import Path

    fake = lambda search_dir: Path("/fake/repo")
    monkeypatch.setattr("commits.git_parser._find_repo_root", fake)
    monkeypatch.setattr("commits.routes._find_repo_root", fake)


@pytest.fixture
def client():
    """Create a test client for the dashboard app."""
    from fastapi.testclient import TestClient

    return TestClient(app_mod.app)


# Sample git log output for mocking
SAMPLE_LOG = (
    "a" * 40
    + "|aaaa1234|Alice|2026-02-20T10:00:00+00:00|Fix bug\n"
    + "\n"
    + " 2 files changed, 10 insertions(+), 3 deletions(-)\n"
    + "b" * 40
    + "|bbbb5678|Bob|2026-02-19T09:00:00+00:00|Add feature\n"
    + "\n"
    + " 1 file changed, 5 insertions(+)\n"
)

SAMPLE_SHOW_META = (
    "a" * 40 + "|aaaa1234|Alice|2026-02-20T10:00:00+00:00|Fix bug|Some body text"
)

SAMPLE_NUMSTAT = "10\t3\tsrc/main.py\n5\t0\tREADME.md\n"

SAMPLE_NAME_STATUS = "M\tsrc/main.py\nA\tREADME.md\n"

SAMPLE_DIFF = (
    "diff --git a/src/main.py b/src/main.py\n"
    "--- a/src/main.py\n"
    "+++ b/src/main.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "-old\n"
    "+new\n"
    " line3\n"
)

SAMPLE_FILE_CONTENT = "line1\nnew\nline3\n"


# The comparison model resolves refs first: "a"*40 is the sample commit,
# "b"*40 its parent, "c"*40 any other ref (branches in the compare tests).
PARENT_HASH = "b" * 40
OTHER_HASH = "c" * 40
MERGE_BASE_HASH = "d" * 40


def _mock_run_git(*args, repo_dir=None, check=True):
    """Mock _run_git based on command arguments.

    Single-commit routes now run through compare.py: the commit is resolved
    with rev-parse, its parent read with ``log -1 --format=%P``, and the
    files, diff and gutters come from ``git diff <parent> <commit>`` rather
    than ``git show``, so the diff branch dispatches on the same flags.
    """
    if args[0] == "rev-parse":
        ref = args[-1].removesuffix("^{commit}")
        if ref.startswith("a" * 40):
            return "a" * 40 + "\n"
        if ref.startswith("unknown"):
            return ""
        return OTHER_HASH + "\n"
    if args[0] == "log":
        if "--format=%P" in args:
            return PARENT_HASH + "\n"
        return SAMPLE_LOG
    if args[0] == "merge-base":
        return MERGE_BASE_HASH + "\n"
    if args[0] == "rev-list":
        return "a" * 40 + "\n" + "b" * 40 + "\n"
    if args[0] == "symbolic-ref":
        return "" if "refs/remotes/origin/HEAD" in args else "main\n"
    if args[0] == "for-each-ref":
        if "refs/heads" in args:
            return "refs/heads/main\nrefs/heads/feature\n"
        return "refs/remotes/origin/main\nrefs/remotes/origin/HEAD\n"
    if args[0] == "ls-files":
        return ""
    if args[0] == "show":
        if "--no-patch" in args:
            return SAMPLE_SHOW_META
    if args[0] == "ls-tree":
        # The file read names the blob with ls-tree -- <path>, then cat-file
        if args[-1] == "nonexistent.py":
            return ""
        return "100644 blob " + "e" * 40 + "\t" + args[-1] + "\0"
    if args[0] == "cat-file":
        return SAMPLE_FILE_CONTENT
    if args[0] == "diff":
        if "--numstat" in args:
            return SAMPLE_NUMSTAT
        if "--name-status" in args:
            return SAMPLE_NAME_STATUS
        return SAMPLE_DIFF
    return ""


# ---------------------------------------------------------------------------
# GET /api/commits
# ---------------------------------------------------------------------------


class TestApiListCommits:
    def test_returns_commits(self, client):
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get("/api/commits")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["short"] == "aaaa1234"
        assert data[0]["message"] == "Fix bug"

    def test_pagination_params(self, client):
        with mock.patch(
            "commits.git_parser._run_git", side_effect=_mock_run_git
        ) as mock_git:
            resp = client.get("/api/commits?skip=10&limit=5")
        assert resp.status_code == 200
        # Verify git was called with correct skip/limit
        call_args = mock_git.call_args_list[0]
        args_str = " ".join(call_args[0])
        assert "--skip=10" in args_str
        assert "--max-count=5" in args_str

    def test_search_param(self, client):
        with mock.patch(
            "commits.git_parser._run_git", side_effect=_mock_run_git
        ) as mock_git:
            resp = client.get("/api/commits?search=fix")
        assert resp.status_code == 200
        call_args = mock_git.call_args_list[0]
        args_str = " ".join(call_args[0])
        assert "--grep=fix" in args_str

    def test_empty_result(self, client):
        with mock.patch("commits.git_parser._run_git", return_value=""):
            resp = client.get("/api/commits")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/commits/<hash>
# ---------------------------------------------------------------------------


class TestApiCommitDetail:
    def test_returns_commit(self, client):
        h = "a" * 40
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get(f"/api/commits/{h}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["hash"] == h
        assert data["message"] == "Fix bug"
        assert len(data["files"]) == 2

    def test_invalid_hash_rejected(self, client):
        resp = client.get("/api/commits/not-a-hash!")
        assert resp.status_code == 400

    def test_short_hash_rejected(self, client):
        resp = client.get("/api/commits/abc")
        assert resp.status_code == 400

    def test_command_injection_rejected(self, client):
        resp = client.get("/api/commits/abcd;rm%20-rf%20/")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/commits/<hash>/diff
# ---------------------------------------------------------------------------


class TestApiCommitDiff:
    def test_returns_diff(self, client):
        h = "a" * 40
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get(f"/api/commits/{h}/diff")
        assert resp.status_code == 200
        data = resp.json()
        assert "files" in data
        assert len(data["files"]) >= 1
        assert data["files"][0]["path"] == "src/main.py"

    def test_invalid_hash(self, client):
        resp = client.get("/api/commits/INVALID/diff")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/commits/<hash>/file/<path>
# ---------------------------------------------------------------------------


class TestApiCommitFile:
    def test_returns_file_with_gutters(self, client):
        h = "a" * 40
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get(f"/api/commits/{h}/file/src/main.py")
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data
        assert "lines" in data
        assert len(data["lines"]) > 0
        assert "gutter" in data["lines"][0]

    def test_path_traversal_rejected(self, client):
        h = "a" * 40
        # Use a path with .. that won't be normalized by URL routing
        resp = client.get(f"/api/commits/{h}/file/src/../../../etc/passwd")
        assert resp.status_code in (
            400,
            404,
        )  # 404 if Starlette normalizes, 400 if our validator catches it

    def test_invalid_hash(self, client):
        resp = client.get("/api/commits/BAD/file/test.py")
        assert resp.status_code == 400

    def test_file_not_found(self, client):
        h = "a" * 40

        # ls-tree answers nothing for a path absent at that commit
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get(f"/api/commits/{h}/file/nonexistent.py")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /api/git/repos
# ---------------------------------------------------------------------------


class TestApiGitRepos:
    def test_returns_repos(self, client):
        fd_output = b"/home/user/project-a/.git\n/home/user/project-b/.git\n"
        with mock.patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = mock.AsyncMock()
            proc.communicate.return_value = (fd_output, b"")
            proc.returncode = 0
            mock_exec.return_value = proc
            import main as _main

            old_fd = _main.FD_BINARY
            _main.FD_BINARY = "fd"
            try:
                resp = client.get("/api/commits/repos")
            finally:
                _main.FD_BINARY = old_fd
        assert resp.status_code == 200
        data = resp.json()
        assert "/home/user/project-a" in data
        assert "/home/user/project-b" in data

    def test_query_filters(self, client):
        fd_output = b"/home/user/merlin/.git\n/home/user/other/.git\n"
        with mock.patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = mock.AsyncMock()
            proc.communicate.return_value = (fd_output, b"")
            proc.returncode = 0
            mock_exec.return_value = proc
            import main as _main

            old_fd = _main.FD_BINARY
            _main.FD_BINARY = "fd"
            try:
                resp = client.get("/api/commits/repos?q=merlin")
            finally:
                _main.FD_BINARY = old_fd
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert "/home/user/merlin" in data

    def test_empty_query_returns_all(self, client):
        fd_output = b"/home/user/a/.git\n/home/user/b/.git\n"
        with mock.patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = mock.AsyncMock()
            proc.communicate.return_value = (fd_output, b"")
            proc.returncode = 0
            mock_exec.return_value = proc
            import main as _main

            old_fd = _main.FD_BINARY
            _main.FD_BINARY = "fd"
            try:
                resp = client.get("/api/commits/repos?q=")
            finally:
                _main.FD_BINARY = old_fd
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_repos_not_shadowed_by_commit_hash_route(self, client):
        """Invariant: /repos must be registered before /{commit_hash} so
        "repos" is matched as a literal, not captured as a commit hash. If the
        route functions are ever reordered, this reaches api_commit_detail and
        fails hash validation (400) instead of the repos handler. With
        FD_BINARY unset the repos handler returns 500 "fd is not available" —
        which is exactly the proof it resolved to api_git_repos, not to
        _validate_hash("repos").
        """
        import main as _main

        old_fd = _main.FD_BINARY
        _main.FD_BINARY = ""
        try:
            resp = client.get("/api/commits/repos")
        finally:
            _main.FD_BINARY = old_fd
        assert resp.status_code == 500
        assert "fd is not available" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


class TestRepoParam:
    """Test ?repo= parameter validation across commit endpoints."""

    def test_valid_repo_returns_commits(self, client, tmp_path):
        """Valid directory path with mocked git root returns commits."""
        # autouse _mock_repo_root already mocks _find_repo_root to return /fake/repo
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get(f"/api/commits?repo={tmp_path}")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_non_git_repo_returns_400(self, client, monkeypatch, tmp_path):
        """Non-git directory returns 400 when _find_repo_root returns None."""
        none_fn = lambda search_dir: None
        monkeypatch.setattr("commits.git_parser._find_repo_root", none_fn)
        monkeypatch.setattr("commits.routes._find_repo_root", none_fn)
        resp = client.get(f"/api/commits?repo={tmp_path}")
        assert resp.status_code == 400

    def test_nonexistent_path_returns_400(self, client):
        """Nonexistent path returns 400."""
        resp = client.get("/api/commits?repo=/nonexistent_xyz_123")
        assert resp.status_code == 400

    def test_empty_repo_uses_startup_cwd(self, client):
        """Empty ?repo= falls back to startup CWD (backward compat)."""
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get("/api/commits")
        assert resp.status_code == 200

    def test_repo_param_on_detail(self, client, tmp_path):
        """?repo= works on commit detail endpoint."""
        h = "a" * 40
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get(f"/api/commits/{h}?repo={tmp_path}")
        assert resp.status_code == 200

    def test_repo_param_on_diff(self, client, tmp_path):
        """?repo= works on commit diff endpoint."""
        h = "a" * 40
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get(f"/api/commits/{h}/diff?repo={tmp_path}")
        assert resp.status_code == 200


class TestPageRoutes:
    def test_commits_page(self, client):
        resp = client.get("/commits")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_commit_detail_page(self, client):
        h = "a" * 40
        resp = client.get(f"/commits/{h}")
        assert resp.status_code == 200

    def test_commit_file_page(self, client):
        h = "a" * 40
        resp = client.get(f"/commits/{h}/file/src/main.py")
        assert resp.status_code == 200

    def test_invalid_hash_page(self, client):
        resp = client.get("/commits/NOT-VALID!")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/commits/compare, /compare/diff, /compare/file, /refs
# ---------------------------------------------------------------------------


class TestApiCompare:
    def test_detail_shape(self, client):
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get("/api/commits/compare?base=main&head=feature&mergebase=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kind"] == "branch"
        assert data["base"] == "main" and data["head"] == "feature"
        assert data["mergebase"] is True and data["worktree"] is False
        assert data["base_resolved"] == OTHER_HASH
        assert data["head_resolved"] == OTHER_HASH
        assert data["merge_base"] == MERGE_BASE_HASH
        assert data["diff_base"] == MERGE_BASE_HASH
        assert data["head_short"] == "c" * 7
        assert data["merge_base_short"] == "d" * 7
        assert data["commit_count"] == 2
        assert data["oldest"]["message"] == "Add feature"
        assert [c["message"] for c in data["commits"]] == ["Fix bug", "Add feature"]
        assert [f["path"] for f in data["files"]] == ["src/main.py", "README.md"]

    def test_worktree_detail(self, client):
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get("/api/commits/compare?worktree=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kind"] == "worktree"
        assert data["head_resolved"] is None
        assert data["base"] == "HEAD"
        assert data["commits"] == [] and data["commit_count"] == 0

    @pytest.mark.parametrize(
        "query",
        [
            "base=--output=/tmp/x&head=main",
            "base=main&head=-p",
            "base=main..x&head=main",
            "base=a%20b&head=main",
            "head=main",
            "base=main",
        ],
    )
    def test_bad_ref_is_400(self, client, query):
        with mock.patch(
            "commits.git_parser._run_git", side_effect=_mock_run_git
        ) as mock_git:
            resp = client.get(f"/api/commits/compare?{query}")
        assert resp.status_code == 400
        # No git call ever saw the rejected value.
        for call in mock_git.call_args_list:
            assert "--output=/tmp/x" not in call[0]
            assert "-p" != call[0][-1]

    def test_unresolvable_ref_is_400(self, client):
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get("/api/commits/compare?base=main&head=unknown-branch")
        assert resp.status_code == 400
        assert "Unknown ref" in resp.json()["detail"]

    def test_refs_are_resolved_before_any_diff(self, client):
        with mock.patch(
            "commits.git_parser._run_git", side_effect=_mock_run_git
        ) as mock_git:
            client.get("/api/commits/compare/diff?base=main&head=feature")
        kinds = [call[0][0] for call in mock_git.call_args_list]
        assert kinds[0] == "rev-parse"
        assert kinds.index("rev-parse") < kinds.index("diff")
        for call in mock_git.call_args_list:
            args = call[0]
            if args[0] == "diff":
                assert "--end-of-options" in args and "--" in args
                # The typed names never reach git diff, only resolved shas.
                assert "main" not in args and "feature" not in args

    def test_diff(self, client):
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get("/api/commits/compare/diff?base=main&head=feature")
        assert resp.status_code == 200
        assert resp.json()["files"][0]["path"] == "src/main.py"

    def test_file(self, client):
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get(
                "/api/commits/compare/file/src/main.py?base=main&head=feature"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == SAMPLE_FILE_CONTENT
        assert "gutter" in data["lines"][0]

    def test_file_bad_path(self, client):
        resp = client.get(
            "/api/commits/compare/file/src/%2e%2e/x?base=main&head=feature"
        )
        assert resp.status_code in (400, 404)

    def test_refs(self, client):
        with mock.patch("commits.git_parser._run_git", side_effect=_mock_run_git):
            resp = client.get("/api/commits/refs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current"] == "main"
        assert data["local"] == ["main", "feature"]
        assert data["remote"] == ["origin/main"]
        assert data["default_base"] == "feature"

    def test_compare_not_shadowed_by_commit_hash_route(self, client):
        """Invariant: /compare and /refs are registered before /{commit_hash}.
        Reordered, "compare" would fail hash validation with a 400. Here a
        missing head is the compare handler's own 400 with its own message."""
        resp = client.get("/api/commits/compare")
        assert resp.status_code == 400
        assert "head ref" in resp.json()["detail"]


class TestComparePageRoutes:
    def test_compare_page(self, client):
        resp = client.get("/commits/compare?base=main&head=feature")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_compare_file_page(self, client):
        resp = client.get("/commits/compare/file/src/main.py?worktree=1")
        assert resp.status_code == 200

    def test_compare_file_page_bad_path(self, client):
        resp = client.get("/commits/compare/file/src/%2e%2e/x")
        assert resp.status_code in (400, 404)
