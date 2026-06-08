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


# run_setup() refreshes skill shims under $HOME — never touch the real one
# in tests. The dedicated shim test below opts back in with a fake HOME.
@pytest.fixture(autouse=True)
def _no_real_home_shims(monkeypatch):
    import cli

    monkeypatch.setattr(cli, "_refresh_skills", lambda: None)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

from cli import build_parser, get_version, run_setup, run_config, cli_main
from cli import _refresh_skills as _real_refresh_skills  # captured pre-stub


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

    def test_config_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["config"])
        assert args.command == "config"
        assert args.key is None

    def test_config_with_key(self):
        parser = build_parser()
        args = parser.parse_args(["config", "notes-dir"])
        assert args.command == "config"
        assert args.key == "notes-dir"


# ---------------------------------------------------------------------------
# Config command
# ---------------------------------------------------------------------------


class TestRunConfig:
    def test_prints_single_key(self, capsys):
        run_config("notes-dir")
        output = capsys.readouterr().out.strip()
        assert output == str(paths.notes_dir())

    def test_prints_skills_user_dir(self, capsys):
        from lib import skills

        run_config("skills-user-dir")
        output = capsys.readouterr().out.strip()
        assert output == str(skills.user_skills_dir())

    def test_prints_all_keys(self, capsys):
        run_config(None)
        output = capsys.readouterr().out
        assert "notes-dir" in output
        assert "home" in output
        assert "version" in output

    def test_unknown_key_exits(self):
        with pytest.raises(SystemExit) as exc_info:
            run_config("nonexistent-key")
        assert exc_info.value.code == 1

    def test_cli_routing_config(self, capsys):
        cli_main(["config", "home"])
        output = capsys.readouterr().out.strip()
        assert output == str(paths.merlin_home())


# ---------------------------------------------------------------------------
# Skills listing
# ---------------------------------------------------------------------------


class TestRunSkills:
    def _make_user_skill(self, name, description):
        from lib import skills

        d = skills.user_skills_dir() / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n"
        )

    def test_lists_sources_shadowing_and_disabled(self, capsys):
        from cli import run_skills

        # A user skill colliding with core 'cron', plus a uniquely named one.
        self._make_user_skill("cron", "User hijack attempt.")
        self._make_user_skill("piano", "Practice coach.")

        run_skills()
        out = capsys.readouterr().out

        # Core skills are listed under the core source.
        assert "core" in out
        assert "self-awareness" in out
        # The user's uniquely named skill is listed under the user source.
        assert "piano" in out
        assert "skills-user" in out
        # The user 'cron' is shown blocked (core takes precedence).
        assert "blocked" in out.lower()
        # merlin-bot is disabled by default: shown disabled, discord inactive.
        assert "discord" in out
        assert "disabled" in out.lower()

    def test_routing_via_cli_main(self, capsys):
        cli_main(["skills"])
        out = capsys.readouterr().out
        assert "precedence order" in out.lower()


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
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5),
        ):
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
        with mock.patch("builtins.input", side_effect=["mypass", "n", "", ""]):
            run_setup(config_path=config)

        assert config.exists()
        content = config.read_text()
        assert "DASHBOARD_PASS=mypass" in content
        assert "TUNNEL_ENABLED=false" in content

    def test_tunnel_enabled(self, tmp_path):
        config = tmp_path / "config.env"
        with mock.patch("builtins.input", side_effect=["pass", "y", "", ""]):
            run_setup(config_path=config)

        content = config.read_text()
        assert "TUNNEL_ENABLED=true" in content

    def test_discord_token_saved(self, tmp_path):
        config = tmp_path / "config.env"
        with mock.patch(
            "builtins.input", side_effect=["pass", "n", "my-bot-token-123", ""]
        ):
            run_setup(config_path=config)

        content = config.read_text()
        assert "DISCORD_BOT_TOKEN=my-bot-token-123" in content

    def test_empty_password_allowed(self, tmp_path):
        config = tmp_path / "config.env"
        with mock.patch("builtins.input", side_effect=["", "n", "", ""]):
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

        with mock.patch("builtins.input", side_effect=["y", "newpass", "n", "", ""]):
            run_setup(config_path=config)

        content = config.read_text()
        assert "DASHBOARD_PASS=newpass" in content

    def test_preserves_extra_keys(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("DASHBOARD_PASS=old\nCUSTOM_KEY=custom_value\n")

        with mock.patch("builtins.input", side_effect=["y", "newpass", "n", "", ""]):
            run_setup(config_path=config)

        content = config.read_text()
        assert "CUSTOM_KEY=custom_value" in content
        assert "DASHBOARD_PASS=newpass" in content

    def test_creates_parent_dirs(self, tmp_path):
        config = tmp_path / "deep" / "nested" / "config.env"
        with mock.patch("builtins.input", side_effect=["pass", "n", "", ""]):
            run_setup(config_path=config)

        assert config.exists()

    def test_config_file_permissions(self, tmp_path):
        """Config file should be created with 0o600 (owner-only) permissions."""
        config = tmp_path / "config.env"
        with mock.patch("builtins.input", side_effect=["secret", "n", "token123", ""]):
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
        with mock.patch("cli.paths") as mock_paths, mock.patch("main.start_server"):
            mock_paths.is_dev_mode.return_value = True
            mock_paths.config_path.return_value = Path("/tmp/exists")
            cli_main(["start", "--dev", "--no-tunnel"])
        mock_paths.set_dev_mode.assert_called_with(True)

    def test_start_passes_args_to_server(self):
        with mock.patch("main.start_server") as m:
            paths.set_dev_mode(True)  # Skip first-run check
            cli_main(["start", "--port", "9999", "--host", "127.0.0.1", "--no-tunnel"])
        m.assert_called_once_with(port=9999, host="127.0.0.1", no_tunnel=True)


# ---------------------------------------------------------------------------
# merlin cron delegation
# ---------------------------------------------------------------------------


class TestCronDelegation:
    def test_cron_routes_to_manage(self, monkeypatch, capsys):
        import cron.manage

        monkeypatch.setattr(cron.manage, "CRON_JOBS_DIR", paths.cron_jobs_dir())
        cli_main(["cron", "list"])
        out = capsys.readouterr().out
        assert '"ok": true' in out
        assert '"jobs"' in out

    def test_cron_help_passes_through(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["cron", "--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "merlin cron" in out
        assert "trigger" in out

    def test_cron_listed_in_core_help(self):
        help_text = build_parser().format_help()
        assert "cron" in help_text
        assert "Manage scheduled cron jobs" in help_text


# ---------------------------------------------------------------------------
# merlin agent
# ---------------------------------------------------------------------------


class TestAgentCommand:
    def test_prints_brain_doc(self, capsys):
        paths.set_dev_mode(True)
        cli_main(["agent"])
        out = capsys.readouterr().out
        assert "Merlin" in out
        assert "merlin --help" in out

    def test_missing_brain_doc_fails(self, tmp_path, monkeypatch, capsys):
        paths.set_dev_mode(False)
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["agent"])
        assert exc_info.value.code == 1
        assert "Brain doc not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# kb / remember top-level aliases
# ---------------------------------------------------------------------------


class TestTopLevelAliases:
    def _capture_dispatch(self, monkeypatch):
        import ext_commands

        calls: list[list[str]] = []

        def fake_dispatch(argv):
            calls.append(argv)
            raise SystemExit(0)

        monkeypatch.setattr(ext_commands, "dispatch", fake_dispatch)
        return calls

    def test_kb_expands_to_notes_kb(self, monkeypatch):
        calls = self._capture_dispatch(monkeypatch)
        with pytest.raises(SystemExit):
            cli_main(["kb", "add", "--title", "X"])
        assert calls == [["notes", "kb", "add", "--title", "X"]]

    def test_remember_expands_to_notes_remember(self, monkeypatch):
        calls = self._capture_dispatch(monkeypatch)
        with pytest.raises(SystemExit):
            cli_main(["remember", "add", "a fact"])
        assert calls == [["notes", "remember", "add", "a fact"]]

    def test_aliases_listed_in_help(self):
        help_text = build_parser().format_help()
        assert "Top-level aliases:" in help_text
        assert "merlin kb" in help_text
        assert "merlin notes kb" in help_text
        assert "merlin remember" in help_text

    def test_alias_names_reserved(self):
        import ext_commands

        assert "kb" in ext_commands.reserved_names()
        assert "remember" in ext_commands.reserved_names()


# ---------------------------------------------------------------------------
# merlin dashboard-url
# ---------------------------------------------------------------------------


class TestDashboardUrl:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for key in (
            "MERLIN_DASHBOARD_URL",
            "TUNNEL_HOSTNAME",
            "DASHBOARD_USER",
            "DASHBOARD_PASS",
        ):
            monkeypatch.delenv(key, raising=False)

    def test_default_localhost(self, capsys):
        cli_main(["dashboard-url"])
        assert capsys.readouterr().out.strip() == "http://localhost:3123"

    def test_tunnel_hostname(self, monkeypatch, capsys):
        monkeypatch.setenv("TUNNEL_HOSTNAME", "merlin.example.com")
        cli_main(["dashboard-url"])
        assert capsys.readouterr().out.strip() == "https://merlin.example.com"

    def test_explicit_override_wins(self, monkeypatch, capsys):
        monkeypatch.setenv("TUNNEL_HOSTNAME", "merlin.example.com")
        monkeypatch.setenv("MERLIN_DASHBOARD_URL", "http://box.example.org:3123")
        cli_main(["dashboard-url"])
        assert capsys.readouterr().out.strip() == "http://box.example.org:3123"

    def test_credentials_embedded(self, monkeypatch, capsys):
        monkeypatch.setenv("DASHBOARD_USER", "admin")
        monkeypatch.setenv("DASHBOARD_PASS", "s3cret")
        cli_main(["dashboard-url"])
        assert capsys.readouterr().out.strip() == "http://admin:s3cret@localhost:3123"

    def test_credentials_quoted(self, monkeypatch, capsys):
        monkeypatch.setenv("DASHBOARD_PASS", "p@ss/word")
        cli_main(["dashboard-url"])
        out = capsys.readouterr().out.strip()
        assert out == "http://admin:p%40ss%2Fword@localhost:3123"

    def test_scheme_less_override_normalized(self, monkeypatch, capsys):
        monkeypatch.setenv("MERLIN_DASHBOARD_URL", "box.example.com:3123")
        monkeypatch.setenv("DASHBOARD_PASS", "pw")
        cli_main(["dashboard-url"])
        out = capsys.readouterr().out.strip()
        assert out == "http://admin:pw@box.example.com:3123"

    def test_scheme_less_bare_host(self, monkeypatch, capsys):
        monkeypatch.setenv("MERLIN_DASHBOARD_URL", "box.example.com")
        cli_main(["dashboard-url"])
        assert capsys.readouterr().out.strip() == "http://box.example.com"

    def test_reads_config_env(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))
        (tmp_path / "config.env").write_text(
            "MERLIN_DASHBOARD_URL=http://home.example.net:3123\nDASHBOARD_PASS=pw\n"
        )
        cli_main(["dashboard-url"])
        out = capsys.readouterr().out.strip()
        assert out == "http://admin:pw@home.example.net:3123"


# ---------------------------------------------------------------------------
# Setup refreshes skills and shims
# ---------------------------------------------------------------------------


class TestSetupSkillRefresh:
    def test_refresh_skills_builds_registry_and_shims(
        self, tmp_path, monkeypatch, capsys
    ):
        import json

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path / "merlin-home"))

        # merlin-bot is disabled by default; enable it so its bot-gated
        # skill (discord) participates in the refresh. (Core skills like cron
        # aggregate regardless of the bot, so they cannot prove bot refresh.)
        state_path = paths.extensions_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"merlin-bot": True}))

        # The autouse fixture stubs cli._refresh_skills; call the original
        # function object captured at import time.
        _real_refresh_skills()
        out = capsys.readouterr().out
        assert "Skills:" in out

        from lib import skills

        # Bot-gated skill exposed through both shim scopes
        assert (home / ".claude" / "skills" / "discord").is_symlink()
        assert (home / ".agents" / "skills" / "discord").is_symlink()
        assert (skills.canonical_dir() / "discord").is_symlink()


class TestLazyExtensionHelp:
    """The extension scan only runs when help will render."""

    def test_non_help_invocations_skip_the_scan(self, monkeypatch, capsys):
        import ext_commands

        def fail():
            raise AssertionError("extension help must not be built here")

        monkeypatch.setattr(ext_commands, "format_extension_help", fail)
        cli_main(["version"])  # Would raise if the scan ran
        assert capsys.readouterr().out.strip() != ""

    def test_help_invocation_includes_extensions(self, tmp_path):
        import os
        import subprocess
        import sys as _sys

        from tests.unit.test_ext_commands import make_command

        ext_dir = tmp_path / "extensions" / "tasks"
        make_command(ext_dir, "add", docstring="Add a task.")

        env = os.environ.copy()
        env["MERLIN_HOME"] = str(tmp_path)
        repo_root = Path(__file__).parent.parent.parent
        result = subprocess.run(
            [_sys.executable, str(repo_root / "cli.py"), "--help"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode == 0
        assert "merlin tasks add" in result.stdout


class TestDelegatedCommandsTable:
    """One table entry yields routing, the help stub, and reservation."""

    def test_delegated_commands_are_core_commands(self):
        import ext_commands
        from cli import DELEGATED_COMMANDS

        assert set(DELEGATED_COMMANDS) <= set(ext_commands.CORE_COMMANDS)

    def test_every_delegated_command_routes_to_its_own_parser(self, capsys):
        from cli import DELEGATED_COMMANDS

        for name in DELEGATED_COMMANDS:
            with pytest.raises(SystemExit) as exc_info:
                cli_main([name, "--help"])
            assert exc_info.value.code == 0
            assert f"merlin {name}" in capsys.readouterr().out
