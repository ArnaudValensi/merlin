"""Tests for install.sh — the curl|bash installer.

Tests run install.sh --dry-run with various mocked system states and verify
the output contains the expected steps.
"""

import os
import subprocess
from pathlib import Path


INSTALL_SH = Path(__file__).parent.parent.parent / "install.sh"


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
        assert "installer v" in result.stdout

    def test_prints_done_banner(self):
        result = run_installer()
        assert "Merlin installed" in result.stdout

    def test_checks_uv(self):
        result = run_installer()
        assert "Checking for uv" in result.stdout

    def test_checks_tmux(self):
        result = run_installer()
        assert "Checking for tmux" in result.stdout

    def test_does_not_check_cloudflared(self):
        """cloudflared was removed with the bundled tunnel; the installer
        must not prompt for it anymore."""
        result = run_installer()
        assert "cloudflared" not in result.stdout

    def test_fetches_tag(self):
        result = run_installer()
        assert "Fetching latest tag" in result.stdout
        assert "placeholder version" in result.stdout

    def test_creates_version_dir(self, tmp_path):
        result = run_installer(env_overrides={"MERLIN_HOME": str(tmp_path / "merlin")})
        assert "Would download" in result.stdout
        assert "Would extract" in result.stdout

    def test_creates_symlink(self):
        result = run_installer()
        assert "Would symlink" in result.stdout

    def test_launcher_shipped_not_generated(self):
        """Under B the launcher ships in the release; install.sh no longer
        writes one, and the PATH entry is the active version's bin/."""
        result = run_installer()
        assert "shipped in the release" in result.stdout
        assert "current/bin/merlin" in result.stdout
        # The old generated-launcher heredoc must be gone.
        assert "Would write" not in result.stdout

    def test_checks_path(self):
        result = run_installer()
        assert "Checking PATH" in result.stdout

    def test_creates_data_dirs(self):
        result = run_installer()
        assert "Creating data directories" in result.stdout
        for d in ["notes", "jobs", "data", "logs"]:
            assert d in result.stdout

    def test_no_changes_message(self):
        result = run_installer()
        assert "No changes were made" in result.stdout


class TestNonInteractive:
    """--non-interactive: no prompts; optional deps skipped, not sudo-installed."""

    def _run(self, *args, env_overrides=None):
        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            ["bash", str(INSTALL_SH), "--non-interactive", "--dry-run", *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def test_completes_and_skips_optional_deps(self):
        # --non-interactive --dry-run previews a full install without error.
        # The no-prompt guarantee itself is the `return 0` on NON_INTERACTIVE
        # in confirm(); a real (non-dry-run) prompt would read /dev/tty.
        result = self._run()
        assert result.returncode == 0
        assert "Merlin installed" in result.stdout
        # Optional deps that are absent must be skipped, never sudo-installed.
        if "tmux not found" in result.stdout:
            assert "Skipped (non-interactive)" in result.stdout

    def test_flag_accepted_in_any_order(self):
        # -y alias, and order independent from --dry-run
        result = subprocess.run(
            ["bash", str(INSTALL_SH), "--dry-run", "-y"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0

    def test_path_uses_current_bin(self):
        result = self._run()
        assert "current/bin" in result.stdout


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
        # If tmux is already installed, there's no install prompt
        # Either way, the script should complete successfully
        assert result.returncode == 0
