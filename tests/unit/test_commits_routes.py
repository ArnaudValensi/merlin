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


def _mock_run_git(*args, repo_dir=None, check=True):
    """Mock _run_git based on command arguments."""
    if args[0] == "log":
        return SAMPLE_LOG
    if args[0] == "show":
        if "--no-patch" in args:
            return SAMPLE_SHOW_META
        if "--numstat" in args:
            return SAMPLE_NUMSTAT
        if "--name-status" in args:
            return SAMPLE_NAME_STATUS
        if "-p" in args:
            return SAMPLE_DIFF
        # git show <hash>:<path>
        for a in args:
            if ":" in a and not a.startswith("-"):
                return SAMPLE_FILE_CONTENT
    if args[0] == "diff":
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

        def mock_git_404(*args, repo_dir=None, check=True):
            for a in args:
                if ":" in a and not a.startswith("-"):
                    from subprocess import CalledProcessError

                    raise CalledProcessError(128, ["git"], "", "fatal: not found")
            return _mock_run_git(*args, repo_dir=repo_dir, check=check)

        with mock.patch("commits.git_parser._run_git", side_effect=mock_git_404):
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
                resp = client.get("/api/git/repos")
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
                resp = client.get("/api/git/repos?q=merlin")
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
                resp = client.get("/api/git/repos?q=")
            finally:
                _main.FD_BINARY = old_fd
        assert resp.status_code == 200
        assert len(resp.json()) == 2


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
