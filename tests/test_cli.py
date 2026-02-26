"""Tests for cli.py — CLI entry point and subcommands."""

import stat
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import paths

# Reset paths state for each test
@pytest.fixture(autouse=True)
def _reset_paths():
    paths._dev_mode_override = None
    yield
    paths._dev_mode_override = None


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

from cli import build_parser, get_version, run_setup, cli_main


class TestArgumentParsing:
    def test_no_args_defaults_to_start(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None  # cli_main treats None as "start"

    def test_start_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["start"])
        assert args.command == "start"

    def test_start_with_port(self):
        parser = build_parser()
        args = parser.parse_args(["start", "--port", "8080"])
        assert args.port == 8080

    def test_start_with_host(self):
        parser = build_parser()
        args = parser.parse_args(["start", "--host", "127.0.0.1"])
        assert args.host == "127.0.0.1"

    def test_start_with_no_tunnel(self):
        parser = build_parser()
        args = parser.parse_args(["start", "--no-tunnel"])
        assert args.no_tunnel is True

    def test_start_with_dev(self):
        parser = build_parser()
        args = parser.parse_args(["start", "--dev"])
        assert args.dev is True

    def test_start_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["start"])
        assert args.port == 3123
        assert args.host == "0.0.0.0"
        assert args.no_tunnel is False
        assert args.dev is False

    def test_version_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["version"])
        assert args.command == "version"

    def test_setup_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["setup"])
        assert args.command == "setup"

    def test_update_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["update"])
        assert args.command == "update"


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


class TestGetVersion:
    def test_dev_mode_uses_git_describe(self):
        paths.set_dev_mode(True)
        version = get_version()
        # In our git repo, git describe should return something
        assert version != "dev"
        assert version != ""

    def test_dev_mode_strips_v_prefix(self):
        paths.set_dev_mode(True)
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(returncode=0, stdout="v1.2.3\n")
            version = get_version()
        assert version == "1.2.3"

    def test_dev_mode_no_v_prefix(self):
        paths.set_dev_mode(True)
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(returncode=0, stdout="0.5.0-3-gabcdef\n")
            version = get_version()
        assert version == "0.5.0-3-gabcdef"

    def test_dev_mode_fallback_to_dev(self):
        paths.set_dev_mode(True)
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(returncode=128, stdout="")
            version = get_version()
        assert version == "dev"

    def test_dev_mode_git_not_found(self):
        paths.set_dev_mode(True)
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            version = get_version()
        assert version == "dev"

    def test_dev_mode_git_timeout(self):
        paths.set_dev_mode(True)
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5)):
            version = get_version()
        assert version == "dev"

    def test_installed_mode_reads_symlink(self, tmp_path, monkeypatch):
        paths.set_dev_mode(False)
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))

        # Create versions/0.3.0/ and current -> versions/0.3.0
        versions_dir = tmp_path / "versions" / "0.3.0"
        versions_dir.mkdir(parents=True)
        current = tmp_path / "current"
        current.symlink_to(versions_dir)

        version = get_version()
        assert version == "0.3.0"

    def test_installed_mode_no_symlink(self, tmp_path, monkeypatch):
        paths.set_dev_mode(False)
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))

        version = get_version()
        assert version == "unknown"


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------


class TestRunSetup:
    def test_creates_config_file(self, tmp_path):
        config = tmp_path / "config.env"
        with mock.patch("builtins.input", side_effect=["mypass", "n", ""]):
            run_setup(config_path=config)

        assert config.exists()
        content = config.read_text()
        assert "DASHBOARD_PASS=mypass" in content
        assert "TUNNEL_ENABLED=false" in content

    def test_tunnel_enabled(self, tmp_path):
        config = tmp_path / "config.env"
        with mock.patch("builtins.input", side_effect=["pass", "y", ""]):
            run_setup(config_path=config)

        content = config.read_text()
        assert "TUNNEL_ENABLED=true" in content

    def test_discord_token_saved(self, tmp_path):
        config = tmp_path / "config.env"
        with mock.patch("builtins.input", side_effect=["pass", "n", "my-bot-token-123"]):
            run_setup(config_path=config)

        content = config.read_text()
        assert "DISCORD_BOT_TOKEN=my-bot-token-123" in content

    def test_empty_password_allowed(self, tmp_path):
        config = tmp_path / "config.env"
        with mock.patch("builtins.input", side_effect=["", "n", ""]):
            run_setup(config_path=config)

        content = config.read_text()
        assert "DASHBOARD_PASS=" in content

    def test_overwrite_prompt_on_existing(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("DASHBOARD_PASS=old\n")

        # User declines overwrite
        with mock.patch("builtins.input", side_effect=["n"]):
            run_setup(config_path=config)

        # Original content preserved
        assert config.read_text() == "DASHBOARD_PASS=old\n"

    def test_overwrite_accepted(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("DASHBOARD_PASS=old\n")

        with mock.patch("builtins.input", side_effect=["y", "newpass", "n", ""]):
            run_setup(config_path=config)

        content = config.read_text()
        assert "DASHBOARD_PASS=newpass" in content

    def test_preserves_extra_keys(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("DASHBOARD_PASS=old\nCUSTOM_KEY=custom_value\n")

        with mock.patch("builtins.input", side_effect=["y", "newpass", "n", ""]):
            run_setup(config_path=config)

        content = config.read_text()
        assert "CUSTOM_KEY=custom_value" in content
        assert "DASHBOARD_PASS=newpass" in content

    def test_creates_parent_dirs(self, tmp_path):
        config = tmp_path / "deep" / "nested" / "config.env"
        with mock.patch("builtins.input", side_effect=["pass", "n", ""]):
            run_setup(config_path=config)

        assert config.exists()

    def test_config_file_permissions(self, tmp_path):
        """Config file should be created with 0o600 (owner-only) permissions."""
        config = tmp_path / "config.env"
        with mock.patch("builtins.input", side_effect=["secret", "n", "token123"]):
            run_setup(config_path=config)

        mode = config.stat().st_mode
        assert mode & stat.S_IROTH == 0, "Config should not be world-readable"
        assert mode & stat.S_IWOTH == 0, "Config should not be world-writable"
        assert mode & stat.S_IRGRP == 0, "Config should not be group-readable"


# ---------------------------------------------------------------------------
# CLI routing
# ---------------------------------------------------------------------------


class TestCliRouting:
    def test_version_prints_to_stdout(self, capsys):
        paths.set_dev_mode(True)
        cli_main(["version"])
        captured = capsys.readouterr()
        assert captured.out.strip() != ""

    def test_update_calls_run_update(self):
        with mock.patch("cli.run_update") as m:
            cli_main(["update"])
        m.assert_called_once()

    def test_setup_calls_run_setup(self):
        with mock.patch("cli.run_setup") as m:
            cli_main(["setup"])
        m.assert_called_once()

    def test_start_sets_dev_mode(self):
        with mock.patch("cli.paths") as mock_paths, \
             mock.patch("main.start_server"):
            mock_paths.is_dev_mode.return_value = True
            mock_paths.config_path.return_value = Path("/tmp/exists")
            cli_main(["start", "--dev", "--no-tunnel"])
        mock_paths.set_dev_mode.assert_called_with(True)

    def test_start_passes_args_to_server(self):
        with mock.patch("main.start_server") as m:
            paths.set_dev_mode(True)  # Skip first-run check
            cli_main(["start", "--port", "9999", "--host", "127.0.0.1", "--no-tunnel"])
        m.assert_called_once_with(port=9999, host="127.0.0.1", no_tunnel=True)
