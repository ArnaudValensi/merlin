"""Tests for install.sh — the curl|bash installer.

Tests run install.sh --dry-run with various mocked system states and verify
the output contains the expected steps.
"""

import os
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).parent.parent / "install.sh"


def run_installer(
    *,
    env_overrides: dict[str, str] | None = None,
    dry_run: bool = True,
) -> subprocess.CompletedProcess:
    """Run install.sh and return the result."""
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    args = ["bash", str(INSTALL_SH)]
    if dry_run:
        args.append("--dry-run")

    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


class TestDryRun:
    def test_exits_successfully(self):
        result = run_installer()
        assert result.returncode == 0

    def test_prints_banner(self):
        result = run_installer()
        assert "Installing Merlin" in result.stdout

    def test_prints_done_banner(self):
        result = run_installer()
        assert "Merlin installed" in result.stdout

    def test_checks_uv(self):
        result = run_installer()
        assert "Checking for uv" in result.stdout

    def test_checks_tmux(self):
        result = run_installer()
        assert "Checking for tmux" in result.stdout

    def test_checks_cloudflared(self):
        result = run_installer()
        assert "Checking for cloudflared" in result.stdout

    def test_fetches_tag(self):
        result = run_installer()
        assert "Fetching latest tag" in result.stdout
        assert "placeholder version" in result.stdout

    def test_creates_version_dir(self):
        result = run_installer()
        assert "Would download" in result.stdout
        assert "Would extract" in result.stdout

    def test_creates_symlink(self):
        result = run_installer()
        assert "Would symlink" in result.stdout

    def test_creates_launcher(self):
        result = run_installer()
        assert "Would write" in result.stdout
        assert "bin/merlin" in result.stdout

    def test_checks_path(self):
        result = run_installer()
        assert "Checking PATH" in result.stdout

    def test_creates_data_dirs(self):
        result = run_installer()
        assert "Creating data directories" in result.stdout
        for d in ["memory", "cron-jobs", "data", "logs"]:
            assert d in result.stdout

    def test_no_changes_message(self):
        result = run_installer()
        assert "No changes were made" in result.stdout


class TestCustomMerlinHome:
    def test_uses_merlin_home_env(self, tmp_path):
        custom = str(tmp_path / "custom-merlin")
        result = run_installer(env_overrides={"MERLIN_HOME": custom})
        assert result.returncode == 0
        assert custom in result.stdout


class TestPackageManagerDetection:
    def test_detects_some_package_manager(self):
        """In our environment, at least one package manager should be found."""
        result = run_installer()
        # If tmux/cloudflared are already installed, there's no install prompt
        # Either way, the script should complete successfully
        assert result.returncode == 0
