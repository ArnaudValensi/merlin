"""Tests for notes/sync.py — git sync watcher for notes directory."""

import inspect

import pytest

from notes import sync

_async = pytest.mark.asyncio(loop_scope="function")


@pytest.fixture(autouse=True)
def _reset_sync_state(monkeypatch):
    """Reset sync module state between tests."""
    sync.conflicted_files = []
    sync._sync_task = None
    sync._pull_task = None
    monkeypatch.delenv("NOTES_GIT_SYNC", raising=False)
    monkeypatch.delenv("NOTES_GIT_REMOTE", raising=False)
    monkeypatch.delenv("NOTES_SYNC_DEBOUNCE", raising=False)
    monkeypatch.delenv("NOTES_SYNC_PULL_INTERVAL", raising=False)


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo for testing."""
    import subprocess

    repo = tmp_path / "notes"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "checkout", "-b", "main"], cwd=repo, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    # Initial commit so the repo isn't empty
    (repo / ".gitkeep").touch()
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return repo


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------


class TestParseSeconds:
    def test_plain_number(self):
        assert sync._parse_seconds("10") == 10.0

    def test_with_s_suffix(self):
        assert sync._parse_seconds("5s") == 5.0

    def test_whitespace(self):
        assert sync._parse_seconds("  5  ") == 5.0

    def test_case_insensitive(self):
        assert sync._parse_seconds("5S") == 5.0

    def test_invalid_returns_default(self):
        assert sync._parse_seconds("fast") == 5.0

    def test_empty_returns_default(self):
        assert sync._parse_seconds("") == 5.0

    def test_custom_default(self):
        assert sync._parse_seconds("bad", default=60.0) == 60.0


# ---------------------------------------------------------------------------
# Git repo initialization
# ---------------------------------------------------------------------------


@_async
class TestEnsureGitRepo:
    async def test_creates_repo_when_missing(self, tmp_path):
        """Initializes a git repo if .git/ doesn't exist."""
        d = tmp_path / "notes"
        await sync._ensure_git_repo(d)
        assert (d / ".git").exists()

    async def test_skips_when_exists(self, git_repo):
        """Does nothing if .git/ already exists (idempotent)."""
        await sync._ensure_git_repo(git_repo)
        # .git dir should not be recreated
        assert (git_repo / ".git").exists()

    async def test_creates_parent_dirs(self, tmp_path):
        """Creates parent directories if they don't exist."""
        d = tmp_path / "deep" / "nested" / "notes"
        await sync._ensure_git_repo(d)
        assert (d / ".git").exists()


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


@_async
class TestHasChanges:
    async def test_no_changes(self, git_repo):
        """Clean repo has no changes."""
        assert await sync._has_changes(git_repo) is False

    async def test_new_file(self, git_repo):
        """Untracked file is detected as a change."""
        (git_repo / "test.md").write_text("hello")
        assert await sync._has_changes(git_repo) is True

    async def test_modified_file(self, git_repo):
        """Modified tracked file is detected."""
        (git_repo / ".gitkeep").write_text("changed")
        assert await sync._has_changes(git_repo) is True

    async def test_deleted_file(self, git_repo):
        """Deleted tracked file is detected."""
        (git_repo / ".gitkeep").unlink()
        assert await sync._has_changes(git_repo) is True


# ---------------------------------------------------------------------------
# Commit all
# ---------------------------------------------------------------------------


@_async
class TestCommitAll:
    async def test_commits_new_file(self, git_repo):
        """New file is staged and committed."""
        (git_repo / "note.md").write_text("content")
        committed = await sync._commit_all(git_repo)
        assert committed is True
        assert await sync._has_changes(git_repo) is False

    async def test_no_changes_returns_false(self, git_repo):
        """Returns False when there's nothing to commit."""
        committed = await sync._commit_all(git_repo)
        assert committed is False

    async def test_commits_multiple_files(self, git_repo):
        """Multiple files are committed in one operation."""
        (git_repo / "a.md").write_text("a")
        (git_repo / "b.md").write_text("b")
        committed = await sync._commit_all(git_repo)
        assert committed is True
        assert await sync._has_changes(git_repo) is False

    async def test_commits_deletions(self, git_repo):
        """Deleted files are included in the commit."""
        (git_repo / ".gitkeep").unlink()
        committed = await sync._commit_all(git_repo)
        assert committed is True
        assert await sync._has_changes(git_repo) is False


# ---------------------------------------------------------------------------
# Sync cycle
# ---------------------------------------------------------------------------


@_async
class TestSyncCycle:
    async def test_no_changes_no_commit(self, git_repo):
        """Sync cycle with no changes does nothing."""
        result = await sync._sync_cycle(git_repo)
        assert result is False

    async def test_changes_are_committed(self, git_repo):
        """Sync cycle commits changes."""
        (git_repo / "note.md").write_text("content")
        result = await sync._sync_cycle(git_repo)
        assert result is True
        assert await sync._has_changes(git_repo) is False


# ---------------------------------------------------------------------------
# Start/stop sync
# ---------------------------------------------------------------------------


@_async
class TestStartSync:
    async def test_disabled_by_default(self, tmp_path):
        """Sync does not start when NOTES_GIT_SYNC is not set."""
        await sync.start_sync(tmp_path / "notes")
        assert sync._sync_task is None
        assert sync._pull_task is None

    async def test_enabled_starts_tasks(self, tmp_path, monkeypatch):
        """When enabled, starts watcher and puller tasks."""
        monkeypatch.setenv("NOTES_GIT_SYNC", "true")
        d = tmp_path / "notes"
        await sync.start_sync(d)
        assert sync._sync_task is not None
        assert sync._pull_task is not None
        assert (d / ".git").exists()
        # Clean up
        await sync.stop_sync()

    async def test_stop_cancels_tasks(self, tmp_path, monkeypatch):
        """stop_sync cancels running tasks."""
        monkeypatch.setenv("NOTES_GIT_SYNC", "true")
        await sync.start_sync(tmp_path / "notes")
        assert sync._sync_task is not None
        await sync.stop_sync()
        assert sync._sync_task is None
        assert sync._pull_task is None

    async def test_configures_remote(self, tmp_path, monkeypatch):
        """Remote is configured when NOTES_GIT_REMOTE is set."""
        monkeypatch.setenv("NOTES_GIT_SYNC", "true")
        monkeypatch.setenv("NOTES_GIT_REMOTE", "https://github.com/test/notes.git")
        d = tmp_path / "notes"
        await sync.start_sync(d)
        assert await sync._has_remote(d)
        await sync.stop_sync()


# ---------------------------------------------------------------------------
# Remote configuration
# ---------------------------------------------------------------------------


@_async
class TestConfigureRemote:
    async def test_adds_remote(self, git_repo):
        """Adds origin remote when none exists."""
        await sync._configure_remote(git_repo, "https://github.com/test/notes.git")
        assert await sync._has_remote(git_repo)

    async def test_updates_existing_remote(self, git_repo):
        """Updates origin URL when it already exists."""
        await sync._configure_remote(git_repo, "https://github.com/test/old.git")
        await sync._configure_remote(git_repo, "https://github.com/test/new.git")
        rc, url, _ = await sync._run_git("remote", "get-url", "origin", cwd=git_repo)
        assert url == "https://github.com/test/new.git"

    async def test_idempotent_same_url(self, git_repo):
        """No-op when remote already has the correct URL."""
        url = "https://github.com/test/notes.git"
        await sync._configure_remote(git_repo, url)
        await sync._configure_remote(git_repo, url)  # Should not error
        assert await sync._has_remote(git_repo)


# ---------------------------------------------------------------------------
# EXTENSION_META
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Merge conflict handling
# ---------------------------------------------------------------------------


@_async
class TestMergeConflicts:
    @pytest.fixture
    def two_repos(self, tmp_path):
        """Create a 'remote' bare repo and a 'local' clone for conflict testing."""
        import subprocess

        def _git(*args, cwd):
            subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)

        # Create bare remote with main as default branch
        remote = tmp_path / "remote.git"
        remote.mkdir()
        _git("init", "--bare", "-b", "main", cwd=remote)

        # Clone to local
        local = tmp_path / "local"
        subprocess.run(
            ["git", "clone", str(remote), str(local)],
            capture_output=True,
            check=True,
        )
        _git("config", "user.email", "test@test.com", cwd=local)
        _git("config", "user.name", "Test", cwd=local)
        _git("checkout", "-b", "main", cwd=local)

        # Initial commit
        (local / "note.md").write_text("original content\n")
        _git("add", "-A", cwd=local)
        _git("commit", "-m", "init", cwd=local)
        _git("push", "-u", "origin", "main", cwd=local)

        # Clone a second copy (simulates another environment / Obsidian)
        other = tmp_path / "other"
        subprocess.run(
            ["git", "clone", str(remote), str(other)],
            capture_output=True,
            check=True,
        )
        _git("config", "user.email", "other@test.com", cwd=other)
        _git("config", "user.name", "Other", cwd=other)

        return local, other, remote

    async def test_conflict_detected_after_pull(self, two_repos):
        """Divergent edits to the same file produce conflict markers."""
        import subprocess

        local, other, remote = two_repos

        # Other edits and pushes
        (other / "note.md").write_text("other's change\n")
        subprocess.run(["git", "add", "-A"], cwd=other, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "other edit"],
            cwd=other,
            capture_output=True,
            check=True,
        )
        subprocess.run(["git", "push"], cwd=other, capture_output=True, check=True)

        # Local edits (diverges from remote)
        (local / "note.md").write_text("local's change\n")
        await sync._commit_all(local)

        # Pull should detect conflict
        sync.conflicted_files = []
        await sync._pull(local)

        assert len(sync.conflicted_files) > 0
        assert "note.md" in sync.conflicted_files

    async def test_conflicted_file_has_markers(self, two_repos):
        """The conflicted file contains both sides' changes."""
        import subprocess

        local, other, remote = two_repos

        # Create divergent edits
        (other / "note.md").write_text("other version\n")
        subprocess.run(["git", "add", "-A"], cwd=other, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "other"], cwd=other, capture_output=True, check=True
        )
        subprocess.run(["git", "push"], cwd=other, capture_output=True, check=True)

        (local / "note.md").write_text("local version\n")
        await sync._commit_all(local)
        await sync._pull(local)

        content = (local / "note.md").read_text()
        assert "<<<<<<<" in content
        assert ">>>>>>>" in content

    async def test_conflict_auto_committed(self, two_repos):
        """After conflict, repo is not left in a broken merge state."""
        import subprocess

        local, other, remote = two_repos

        (other / "note.md").write_text("other\n")
        subprocess.run(["git", "add", "-A"], cwd=other, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "other"], cwd=other, capture_output=True, check=True
        )
        subprocess.run(["git", "push"], cwd=other, capture_output=True, check=True)

        (local / "note.md").write_text("local\n")
        await sync._commit_all(local)
        await sync._pull(local)

        # Repo should be clean (conflict committed)
        assert await sync._has_changes(local) is False

    async def test_conflict_cleared_after_resolution(self, two_repos):
        """Saving a resolved file clears the conflict for that file."""
        import subprocess

        local, other, remote = two_repos

        (other / "note.md").write_text("other\n")
        subprocess.run(["git", "add", "-A"], cwd=other, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "other"], cwd=other, capture_output=True, check=True
        )
        subprocess.run(["git", "push"], cwd=other, capture_output=True, check=True)

        (local / "note.md").write_text("local\n")
        await sync._commit_all(local)
        await sync._pull(local)
        assert len(sync.conflicted_files) > 0

        # User resolves the conflict
        (local / "note.md").write_text("resolved content\n")
        await sync._update_conflict_state(local)
        assert len(sync.conflicted_files) == 0


# ---------------------------------------------------------------------------
# Sync status API
# ---------------------------------------------------------------------------


class TestSyncStatusAPI:
    """Test the sync status endpoint function directly."""

    def test_no_conflicts(self):
        """Returns empty conflicts when none exist."""
        from notes.routes import api_sync_status

        sync.conflicted_files = []
        result = api_sync_status()
        assert result["has_conflicts"] is False
        assert result["conflicted_files"] == []

    def test_with_conflicts(self):
        """Returns conflicted files when they exist."""
        from notes.routes import api_sync_status

        sync.conflicted_files = ["note.md", "kb/topic.md"]
        result = api_sync_status()
        assert result["has_conflicts"] is True
        assert result["conflicted_files"] == ["note.md", "kb/topic.md"]
        sync.conflicted_files = []


# ---------------------------------------------------------------------------
# EXTENSION_META
# ---------------------------------------------------------------------------


class TestExtensionMeta:
    def test_extension_meta_exists(self):
        """Notes extension defines EXTENSION_META."""
        from notes import EXTENSION_META

        assert EXTENSION_META is not None
        assert EXTENSION_META["name"] == "Notes"

    def test_config_fields_defined(self):
        """EXTENSION_META has the expected config fields."""
        from notes import EXTENSION_META

        keys = {f["key"] for f in EXTENSION_META["config_fields"]}
        assert keys == {
            "NOTES_DIR",
            "NOTES_GIT_SYNC",
            "NOTES_GIT_REMOTE",
            "NOTES_SYNC_DEBOUNCE",
            "NOTES_SYNC_PULL_INTERVAL",
        }

    def test_has_start_hook(self):
        """Notes extension exports start() function."""
        import notes

        assert hasattr(notes, "start")
        assert inspect.iscoroutinefunction(notes.start)
