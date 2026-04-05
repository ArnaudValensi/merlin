"""Tests for paths.py — centralized path resolution."""

import os
from pathlib import Path

import pytest

import paths


@pytest.fixture(autouse=True)
def _reset_paths(monkeypatch, tmp_path):
    """Reset paths module state before each test."""
    # Clear the dev mode override
    paths._dev_mode_override = None
    # Clear env vars that affect path resolution
    monkeypatch.delenv("MERLIN_DEV", raising=False)
    monkeypatch.delenv("MERLIN_HOME", raising=False)
    monkeypatch.delenv("NOTES_DIR", raising=False)
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    monkeypatch.delenv("MERLIN_ENVIRONMENT_SLUG", raising=False)
    yield
    # Clean up after test
    paths._dev_mode_override = None


# ---------------------------------------------------------------------------
# Dev mode detection
# ---------------------------------------------------------------------------


class TestIsDevMode:
    def test_explicit_override_true(self):
        paths.set_dev_mode(True)
        assert paths.is_dev_mode() is True

    def test_explicit_override_false(self):
        paths.set_dev_mode(False)
        assert paths.is_dev_mode() is False

    def test_explicit_override_takes_priority_over_env(self, monkeypatch):
        monkeypatch.setenv("MERLIN_DEV", "1")
        paths.set_dev_mode(False)
        assert paths.is_dev_mode() is False

    def test_env_var_merlin_dev_1(self, monkeypatch):
        monkeypatch.setenv("MERLIN_DEV", "1")
        assert paths.is_dev_mode() is True

    def test_env_var_merlin_dev_true(self, monkeypatch):
        monkeypatch.setenv("MERLIN_DEV", "true")
        assert paths.is_dev_mode() is True

    def test_env_var_merlin_dev_yes(self, monkeypatch):
        monkeypatch.setenv("MERLIN_DEV", "yes")
        assert paths.is_dev_mode() is True

    def test_env_var_merlin_dev_0_forces_installed(self, monkeypatch):
        monkeypatch.setenv("MERLIN_DEV", "0")
        assert paths.is_dev_mode() is False

    def test_env_var_merlin_dev_false_forces_installed(self, monkeypatch):
        monkeypatch.setenv("MERLIN_DEV", "false")
        assert paths.is_dev_mode() is False

    def test_env_var_merlin_dev_no_forces_installed(self, monkeypatch):
        monkeypatch.setenv("MERLIN_DEV", "no")
        assert paths.is_dev_mode() is False

    def test_git_dir_detection(self):
        """In this repo, .git/ exists so dev mode should be True."""
        paths._dev_mode_override = None
        assert paths.is_dev_mode() is True


# ---------------------------------------------------------------------------
# Dev mode paths (running from git checkout)
# ---------------------------------------------------------------------------


class TestDevModePaths:
    @pytest.fixture(autouse=True)
    def _force_dev(self):
        paths.set_dev_mode(True)

    def test_app_dir_is_repo_root(self):
        assert paths.app_dir() == paths._THIS_DIR

    def test_data_dir_is_merlin_home(self):
        """Data always lives in ~/.merlin/, even in dev mode."""
        assert paths.data_dir() == paths.merlin_home()

    def test_config_path(self):
        assert paths.config_path() == paths.merlin_home() / "config.env"

    def test_bot_config_path(self):
        assert paths.bot_config_path() == paths.merlin_home() / "config.env"

    def test_notes_dir(self):
        assert paths.notes_dir() == paths.merlin_home() / "notes"

    def test_cron_jobs_dir(self):
        assert paths.cron_jobs_dir() == paths.merlin_home() / "cron-jobs"

    def test_logs_dir(self):
        assert paths.logs_dir() == paths.merlin_home() / "logs"


# ---------------------------------------------------------------------------
# Installed mode paths (running from ~/.merlin/current/)
# ---------------------------------------------------------------------------


class TestInstalledModePaths:
    @pytest.fixture(autouse=True)
    def _force_installed(self, monkeypatch, tmp_path):
        paths.set_dev_mode(False)
        self.fake_home = tmp_path / ".merlin"
        monkeypatch.setenv("MERLIN_HOME", str(self.fake_home))

    def test_merlin_home(self):
        assert paths.merlin_home() == self.fake_home

    def test_app_dir(self):
        assert paths.app_dir() == self.fake_home / "current"

    def test_data_dir(self):
        assert paths.data_dir() == self.fake_home

    def test_config_path(self):
        assert paths.config_path() == self.fake_home / "config.env"

    def test_bot_config_path_is_same_as_config(self):
        """In installed mode, there's a single config file."""
        assert paths.bot_config_path() == self.fake_home / "config.env"

    def test_notes_dir(self):
        assert paths.notes_dir() == self.fake_home / "notes"

    def test_cron_jobs_dir(self):
        assert paths.cron_jobs_dir() == self.fake_home / "cron-jobs"

    def test_logs_dir(self):
        assert paths.logs_dir() == self.fake_home / "logs"


# ---------------------------------------------------------------------------
# MERLIN_HOME env var override
# ---------------------------------------------------------------------------


class TestMerlinHomeOverride:
    def test_default_is_home_dot_merlin(self, monkeypatch):
        monkeypatch.delenv("MERLIN_HOME", raising=False)
        assert paths.merlin_home() == (Path.home() / ".merlin").resolve()

    def test_custom_merlin_home(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom-merlin"
        monkeypatch.setenv("MERLIN_HOME", str(custom))
        assert paths.merlin_home() == custom

    def test_merlin_home_resolves_relative_path(self, monkeypatch, tmp_path):
        """MERLIN_HOME should resolve to an absolute path."""
        monkeypatch.setenv("MERLIN_HOME", "./relative/path")
        result = paths.merlin_home()
        assert result.is_absolute()

    def test_installed_paths_use_custom_home(self, monkeypatch, tmp_path):
        custom = tmp_path / "my-merlin"
        monkeypatch.setenv("MERLIN_HOME", str(custom))
        paths.set_dev_mode(False)

        assert paths.app_dir() == custom / "current"
        assert paths.data_dir() == custom
        assert paths.config_path() == custom / "config.env"
        assert paths.notes_dir() == custom / "notes"
        assert paths.cron_jobs_dir() == custom / "cron-jobs"
        assert paths.logs_dir() == custom / "logs"


# ---------------------------------------------------------------------------
# Configurable notes_dir
# ---------------------------------------------------------------------------


class TestConfigurableMemoryDir:
    def test_default_standalone(self, monkeypatch):
        """Without NOTES_DIR or MERLIN_ENVIRONMENT_SLUG, returns ~/.merlin/notes."""
        assert paths.notes_dir() == paths.data_dir() / "notes"

    def test_managed_container_default(self, monkeypatch):
        """With MERLIN_ENVIRONMENT_SLUG set, defaults to ~/shared/notes."""
        monkeypatch.setenv("MERLIN_ENVIRONMENT_SLUG", "my-env")
        result = paths.notes_dir()
        assert result == (Path.home() / "shared" / "notes").resolve()

    def test_byoi_with_token_uses_standard_default(self, monkeypatch):
        """BYOI with MERLIN_SAAS_TOKEN but no SLUG uses standard default."""
        monkeypatch.setenv("MERLIN_SAAS_TOKEN", "tok_test")
        assert paths.notes_dir() == paths.data_dir() / "notes"

    def test_notes_dir_env_overrides_default(self, monkeypatch, tmp_path):
        """NOTES_DIR env var overrides the standalone default."""
        custom = tmp_path / "my-notes"
        monkeypatch.setenv("NOTES_DIR", str(custom))
        assert paths.notes_dir() == custom

    def test_notes_dir_env_overrides_managed(self, monkeypatch, tmp_path):
        """NOTES_DIR env var overrides the managed container default too."""
        custom = tmp_path / "my-notes"
        monkeypatch.setenv("MERLIN_ENVIRONMENT_SLUG", "my-env")
        monkeypatch.setenv("NOTES_DIR", str(custom))
        assert paths.notes_dir() == custom

    def test_notes_dir_expands_tilde(self, monkeypatch):
        """NOTES_DIR with ~ is expanded."""
        monkeypatch.setenv("NOTES_DIR", "~/my-notes")
        result = paths.notes_dir()
        assert "~" not in str(result)
        assert result.is_absolute()

    def test_notes_dir_resolves_to_absolute(self, monkeypatch):
        """NOTES_DIR with relative path resolves to absolute."""
        monkeypatch.setenv("NOTES_DIR", "relative/path")
        result = paths.notes_dir()
        assert result.is_absolute()


# ---------------------------------------------------------------------------
# Graceful behavior (first run — ~/.merlin/ doesn't exist yet)
# ---------------------------------------------------------------------------


class TestFirstRun:
    def test_paths_resolve_even_if_dir_missing(self, monkeypatch, tmp_path):
        """Path functions return paths even if the directories don't exist yet."""
        fake_home = tmp_path / "nonexistent" / ".merlin"
        monkeypatch.setenv("MERLIN_HOME", str(fake_home))
        paths.set_dev_mode(False)

        # All path functions should return valid Path objects without errors
        assert isinstance(paths.merlin_home(), Path)
        assert isinstance(paths.app_dir(), Path)
        assert isinstance(paths.data_dir(), Path)
        assert isinstance(paths.config_path(), Path)
        assert isinstance(paths.notes_dir(), Path)
        assert isinstance(paths.cron_jobs_dir(), Path)
        assert isinstance(paths.logs_dir(), Path)

        # None of these directories exist
        assert not fake_home.exists()


# ---------------------------------------------------------------------------
# set_dev_mode
# ---------------------------------------------------------------------------


class TestSetDevMode:
    def test_set_true(self):
        paths.set_dev_mode(True)
        assert paths.is_dev_mode() is True

    def test_set_false(self):
        paths.set_dev_mode(False)
        assert paths.is_dev_mode() is False

    def test_overrides_git_detection(self):
        """Even in a git repo, set_dev_mode(False) forces installed mode."""
        assert (paths._THIS_DIR / ".git").exists()  # We are in a git repo (or submodule)
        paths.set_dev_mode(False)
        assert paths.is_dev_mode() is False


# ---------------------------------------------------------------------------
# Integration: verify migrated modules use paths module
# ---------------------------------------------------------------------------


class TestModuleIntegration:
    """Verify that migrated modules resolve paths through the paths module.

    Module-level constants are cached at import time. Test isolation sets
    MERLIN_HOME to a temp dir before imports, so these constants should NOT
    be under ~/.merlin/ — proving the modules used paths.py for resolution.
    """

    _default_home = str((Path.home() / ".merlin").resolve())

    def test_claude_wrapper_uses_paths(self):
        import lib.claude as cw
        assert not str(cw.LOG_DIR).startswith(self._default_home)
        assert cw.LOG_DIR.name == "claude"
        assert cw.LOG_DIR.parent.name == "logs"
        assert cw.RAW_SESSION_DIR.name == "raw-sessions"
        assert cw.RAW_SESSION_DIR.parent == cw.LOG_DIR.parent

    def test_structured_log_uses_paths(self):
        import structured_log as sl
        assert not str(sl.ENGINE_LOG_PATH).startswith(self._default_home)
        assert sl.ENGINE_LOG_PATH.name == "engine-log.jsonl"
        assert sl.ENGINE_LOG_PATH.parent.name == "logs"

    def test_session_registry_uses_paths(self):
        import session_registry as sr
        assert not str(sr.DATA_DIR).startswith(self._default_home)
        assert sr.DATA_DIR.name == "data"

    def test_cron_state_uses_paths(self):
        from cron.state import CRON_JOBS_DIR, STATE_DIR, LOCKS_DIR, HISTORY_FILE
        assert not str(CRON_JOBS_DIR).startswith(self._default_home)
        assert CRON_JOBS_DIR.name == "cron-jobs"
        assert STATE_DIR == CRON_JOBS_DIR / ".state"
        assert LOCKS_DIR == CRON_JOBS_DIR / ".locks"
        assert HISTORY_FILE == CRON_JOBS_DIR / ".history.json"

    def test_notes_routes_uses_paths(self):
        from notes.routes import _notes_dir, _media_dir
        assert _notes_dir() == paths.notes_dir()
        assert _media_dir() == paths.notes_dir() / "media"

    def test_all_paths_are_absolute(self):
        """All path functions return absolute paths."""
        for fn in [paths.merlin_home, paths.app_dir, paths.data_dir,
                    paths.config_path, paths.bot_config_path,
                    paths.notes_dir, paths.cron_jobs_dir, paths.logs_dir]:
            result = fn()
            assert result.is_absolute(), f"{fn.__name__}() returned non-absolute: {result}"

    def test_dev_mode_app_dir_exists_on_disk(self):
        """In dev mode, app_dir should point to the repo root (which exists)."""
        paths.set_dev_mode(True)
        assert paths.app_dir().is_dir()
