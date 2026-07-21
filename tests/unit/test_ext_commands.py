"""Tests for ext_commands.py — extension command discovery and dispatch."""

import os
import stat
from pathlib import Path

import pytest

import ext_commands
import paths


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_command(
    ext_dir: Path,
    name: str,
    *,
    body: str = "",
    docstring: str | None = "Do the thing.\n\nLong details here.",
    executable: bool = True,
    suffix: str = ".py",
) -> Path:
    """Create a command file under <ext_dir>/commands/."""
    commands_dir = ext_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    file = commands_dir / f"{name}{suffix}"

    if suffix == ".py":
        parts = ["#!/usr/bin/env python3"]
        if docstring is not None:
            parts.append(f'"""{docstring}"""')
        parts.append(body)
        file.write_text("\n".join(parts) + "\n")
    else:
        file.write_text(body)

    if executable:
        file.chmod(file.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return file


@pytest.fixture
def tasks_ext(tmp_path) -> Path:
    """A fixture 'tasks' extension under the tmp MERLIN_HOME."""
    ext_dir = paths.extensions_dir() / "tasks"
    make_command(ext_dir, "add", docstring="Add a task to the list.")
    make_command(ext_dir, "recap", docstring="Print the daily recap.")
    return ext_dir


# ---------------------------------------------------------------------------
# extract_help
# ---------------------------------------------------------------------------


class TestExtractHelp:
    def test_first_docstring_line(self, tmp_path):
        file = make_command(tmp_path / "x", "cmd", docstring="One line.\nSecond line.")
        assert ext_commands.extract_help(file) == "One line."

    def test_multiline_docstring_first_line_only(self, tmp_path):
        file = make_command(
            tmp_path / "x", "cmd", docstring="Summary here.\n\nDetails below."
        )
        assert ext_commands.extract_help(file) == "Summary here."

    def test_missing_docstring(self, tmp_path):
        file = make_command(tmp_path / "x", "cmd", docstring=None, body="x = 1")
        assert ext_commands.extract_help(file) is None

    def test_syntax_error_degrades_to_none(self, tmp_path):
        file = make_command(
            tmp_path / "x", "cmd", docstring=None, body="def broken(:\n  pass"
        )
        assert ext_commands.extract_help(file) is None

    def test_non_utf8_degrades_to_none(self, tmp_path):
        commands_dir = tmp_path / "x" / "commands"
        commands_dir.mkdir(parents=True)
        file = commands_dir / "cmd.py"
        file.write_bytes(b'"""\xff\xfe broken encoding"""')
        assert ext_commands.extract_help(file) is None

    def test_missing_file_degrades_to_none(self, tmp_path):
        assert ext_commands.extract_help(tmp_path / "nope.py") is None

    def test_empty_docstring(self, tmp_path):
        file = make_command(tmp_path / "x", "cmd", docstring="   \n   ")
        assert ext_commands.extract_help(file) is None

    def test_non_python_description_comment(self, tmp_path):
        file = make_command(
            tmp_path / "x",
            "cmd",
            suffix=".sh",
            body="#!/bin/bash\n# Description: A shell command.\necho hi\n",
        )
        assert ext_commands.extract_help(file) == "A shell command."

    def test_non_python_without_description(self, tmp_path):
        file = make_command(
            tmp_path / "x", "cmd", suffix=".sh", body="#!/bin/bash\necho hi\n"
        )
        assert ext_commands.extract_help(file) is None


# ---------------------------------------------------------------------------
# list_commands
# ---------------------------------------------------------------------------


class TestListCommands:
    def test_lists_executables_by_stem(self, tasks_ext):
        commands = ext_commands.list_commands(tasks_ext)
        assert sorted(commands) == ["add", "recap"]
        assert commands["add"].name == "add.py"

    def test_skips_non_executable(self, tmp_path):
        ext_dir = tmp_path / "ext"
        make_command(ext_dir, "runnable")
        make_command(ext_dir, "plain", executable=False)
        assert sorted(ext_commands.list_commands(ext_dir)) == ["runnable"]

    def test_skips_dotfiles_and_dirs(self, tmp_path):
        ext_dir = tmp_path / "ext"
        make_command(ext_dir, "real")
        (ext_dir / "commands" / ".hidden.py").write_text("x")
        (ext_dir / "commands" / "subdir").mkdir()
        assert sorted(ext_commands.list_commands(ext_dir)) == ["real"]

    def test_missing_commands_dir(self, tmp_path):
        assert ext_commands.list_commands(tmp_path / "nothing") == {}

    def test_skips_underscore_prefixed_files(self, tmp_path):
        ext_dir = tmp_path / "ext"
        make_command(ext_dir, "real")
        make_command(ext_dir, "__init__")  # executable or not, never a command
        make_command(ext_dir, "_helper")
        assert sorted(ext_commands.list_commands(ext_dir)) == ["real"]

    def test_stem_collision_first_sorted_wins(self, tmp_path):
        ext_dir = tmp_path / "ext"
        make_command(ext_dir, "add", suffix=".py")
        make_command(ext_dir, "add", suffix=".sh", body="#!/bin/bash\n")
        commands = ext_commands.list_commands(ext_dir)
        assert commands["add"].name == "add.py"  # .py < .sh in sorted order


# ---------------------------------------------------------------------------
# Reserved names
# ---------------------------------------------------------------------------


class TestReservedNames:
    def test_core_commands_reserved(self):
        for name in ("start", "version", "setup", "update", "config"):
            assert name in ext_commands.reserved_names()

    def test_builtin_ids_reserved(self):
        for name in ("notes", "merlin-bot", "job", "files", "terminal", "commits"):
            assert name in ext_commands.reserved_names()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_installed_extension_dirs(self, tasks_ext):
        dirs = ext_commands.installed_extension_dirs()
        assert dirs == {"tasks": tasks_ext}

    def test_installed_missing_extensions_dir(self):
        assert ext_commands.installed_extension_dirs() == {}

    def test_builtin_dirs_point_into_app_dir(self):
        dirs = ext_commands.builtin_extension_dirs()
        assert dirs["notes"] == paths.app_dir() / "notes"
        assert dirs["merlin-bot"] == paths.app_dir() / "merlin-bot"


# ---------------------------------------------------------------------------
# format_extension_help
# ---------------------------------------------------------------------------


class TestFormatExtensionHelp:
    def test_installed_group_with_helps(self, tasks_ext):
        text = ext_commands.format_extension_help()
        assert "Installed extensions:" in text
        assert "merlin tasks add" in text
        assert "Add a task to the list." in text

    def test_empty_when_no_commands(self, monkeypatch):
        # The real notes built-in ships commands; stub built-ins out to
        # exercise the no-commands case.
        monkeypatch.setattr(ext_commands, "builtin_extension_dirs", lambda: {})
        assert ext_commands.format_extension_help() == ""

    def test_real_notes_builtin_enumerated(self):
        """The shipped notes built-in appears in the help catalog."""
        text = ext_commands.format_extension_help()
        assert "Built-in extensions:" in text
        assert "merlin notes search" in text
        assert "merlin notes kb" in text
        assert "merlin notes remember" in text

    def test_reserved_installed_extension_skipped_with_warning(
        self, tmp_path, capsys, monkeypatch
    ):
        # Builtin dirs resolve outside tmp MERLIN_HOME — point them elsewhere
        monkeypatch.setattr(ext_commands, "builtin_extension_dirs", lambda: {})
        make_command(paths.extensions_dir() / "job", "evil")
        text = ext_commands.format_extension_help()
        err = capsys.readouterr().err
        assert "merlin job evil" not in text
        assert "reserved" in err
        assert "job" in err

    def test_builtin_group(self, tmp_path, monkeypatch):
        builtin = tmp_path / "builtin-notes"
        make_command(builtin, "search", docstring="Search the notes.")
        monkeypatch.setattr(
            ext_commands, "builtin_extension_dirs", lambda: {"notes": builtin}
        )
        text = ext_commands.format_extension_help()
        assert "Built-in extensions:" in text
        assert "merlin notes search" in text
        assert "Search the notes." in text


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@pytest.fixture
def capture_execv(monkeypatch):
    """Capture os.execv calls instead of replacing the process."""
    calls: list[tuple[str, list[str]]] = []

    def fake_execv(file, argv):
        calls.append((file, argv))
        raise SystemExit(0)  # Simulate process replacement for test flow

    monkeypatch.setattr(os, "execv", fake_execv)
    return calls


class TestDispatch:
    def test_routes_to_command_with_args(self, tasks_ext, capture_execv):
        with pytest.raises(SystemExit):
            ext_commands.dispatch(["tasks", "add", "buy milk", "--due", "friday"])
        file, argv = capture_execv[0]
        assert file == str(tasks_ext / "commands" / "add.py")
        assert argv == [file, "buy milk", "--due", "friday"]

    def test_help_flag_passes_through(self, tasks_ext, capture_execv):
        with pytest.raises(SystemExit):
            ext_commands.dispatch(["tasks", "add", "--help"])
        _, argv = capture_execv[0]
        assert argv[1:] == ["--help"]

    def test_unknown_extension(self, tasks_ext, capsys):
        with pytest.raises(SystemExit) as exc_info:
            ext_commands.dispatch(["nonexistent", "cmd"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "Unknown command: 'nonexistent'" in err
        assert "tasks" in err  # Lists installed extensions as a hint

    def test_unknown_command_lists_available(self, tasks_ext, capsys):
        with pytest.raises(SystemExit) as exc_info:
            ext_commands.dispatch(["tasks", "bogus"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "Unknown command 'bogus' for extension 'tasks'" in err
        assert "add" in err and "recap" in err

    def test_extension_without_subcommand_shows_usage(self, tasks_ext, capsys):
        with pytest.raises(SystemExit) as exc_info:
            ext_commands.dispatch(["tasks"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "usage: merlin tasks <command>" in err
        assert "merlin tasks add" in err

    def test_reserved_installed_extension_rejected(self, capsys, monkeypatch):
        monkeypatch.setattr(ext_commands, "builtin_extension_dirs", lambda: {})
        make_command(paths.extensions_dir() / "job", "evil")
        with pytest.raises(SystemExit) as exc_info:
            ext_commands.dispatch(["job", "evil"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "reserved" in err

    def test_non_executable_command_explains_chmod(self, tmp_path, capsys):
        ext_dir = paths.extensions_dir() / "tasks"
        make_command(ext_dir, "add", executable=False)
        with pytest.raises(SystemExit) as exc_info:
            ext_commands.dispatch(["tasks", "add"])
        assert exc_info.value.code == 126
        err = capsys.readouterr().err
        assert "chmod +x" in err

    def test_builtin_commands_win_over_installed(
        self, tmp_path, capture_execv, monkeypatch
    ):
        builtin = tmp_path / "builtin-notes"
        builtin_file = make_command(builtin, "search")
        monkeypatch.setattr(
            ext_commands, "builtin_extension_dirs", lambda: {"notes": builtin}
        )
        make_command(paths.extensions_dir() / "notes", "search")
        with pytest.raises(SystemExit):
            ext_commands.dispatch(["notes", "search"])
        file, _ = capture_execv[0]
        assert file == str(builtin_file)


# ---------------------------------------------------------------------------
# CLI integration: routing and grouped help
# ---------------------------------------------------------------------------


class TestCliIntegration:
    def test_core_commands_match_parser(self):
        """Drift guard: ext_commands.CORE_COMMANDS mirrors the argparse tree."""
        import argparse

        from cli import build_parser

        parser = build_parser()
        subparsers = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        assert set(subparsers.choices) == set(ext_commands.CORE_COMMANDS)

    def test_help_shows_three_groups(self, tmp_path, monkeypatch):
        from cli import build_parser

        builtin = tmp_path / "builtin-notes"
        make_command(builtin, "search", docstring="Search the notes.")
        monkeypatch.setattr(
            ext_commands, "builtin_extension_dirs", lambda: {"notes": builtin}
        )
        make_command(paths.extensions_dir() / "tasks", "add", docstring="Add a task.")

        help_text = build_parser().format_help()
        assert "Core commands:" in help_text
        assert "Built-in extensions:" in help_text
        assert "merlin notes search" in help_text
        assert "Search the notes." in help_text
        assert "Installed extensions:" in help_text
        assert "merlin tasks add" in help_text
        assert "Add a task." in help_text

    def test_cli_main_routes_unknown_token_to_dispatch(self, monkeypatch):
        from cli import cli_main

        called: list[list[str]] = []

        def fake_dispatch(argv):
            called.append(argv)
            raise SystemExit(0)

        monkeypatch.setattr(ext_commands, "dispatch", fake_dispatch)
        with pytest.raises(SystemExit):
            cli_main(["tasks", "add", "--due", "friday"])
        assert called == [["tasks", "add", "--due", "friday"]]

    def test_cli_main_core_command_not_dispatched(self, monkeypatch, capsys):
        from cli import cli_main

        monkeypatch.setattr(
            ext_commands,
            "dispatch",
            lambda argv: pytest.fail("core command must not hit dispatch"),
        )
        cli_main(["version"])
        assert capsys.readouterr().out.strip() != ""


# ---------------------------------------------------------------------------
# Server load: reserved extension names rejected
# ---------------------------------------------------------------------------


class TestServerLoadRejection:
    def test_reserved_installed_extension_registered_as_error(self, tmp_path):
        import main

        make_command(paths.extensions_dir() / "job", "evil")
        make_command(paths.extensions_dir() / "merlin-bot", "evil")

        saved_registry = dict(main.extension_registry)
        try:
            main.extension_registry.pop("job", None)
            main.extension_registry.pop("merlin-bot", None)
            main._load_installed_extensions()

            for name in ("job", "merlin-bot"):
                info = main.extension_registry[name]
                assert info.tier == "installed"
                assert info.loaded is False
                assert info.enabled is False
                assert "reserved" in (info.error or "")
        finally:
            main.extension_registry.clear()
            main.extension_registry.update(saved_registry)


# ---------------------------------------------------------------------------
# End-to-end pass-through (real subprocess through cli.py)
# ---------------------------------------------------------------------------


ECHO_BODY = """\
import sys
print("args:" + "|".join(sys.argv[1:]))
print("err-line", file=sys.stderr)
sys.exit(7)
"""

PEP723_SCRIPT = """\
#!/usr/bin/env -S uv run --script
# /// script
# dependencies = []
# ///
\"\"\"PEP 723 fixture command.\"\"\"
import sys
print("pep723-ok:" + "|".join(sys.argv[1:]))
"""


def run_cli(argv: list[str], merlin_home: Path):
    """Run cli.py in a subprocess with MERLIN_HOME pointing at a tmp dir."""
    import subprocess
    import sys as _sys

    env = os.environ.copy()
    env["MERLIN_HOME"] = str(merlin_home)
    repo_root = Path(__file__).parent.parent.parent
    return subprocess.run(
        [_sys.executable, str(repo_root / "cli.py"), *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


class TestEndToEnd:
    def test_args_exit_code_and_stdio_pass_through(self, tmp_path):
        ext_dir = tmp_path / "extensions" / "tasks"
        make_command(ext_dir, "echoargs", docstring="Echo args.", body=ECHO_BODY)

        result = run_cli(["tasks", "echoargs", "one", "--flag", "two"], tmp_path)
        assert result.returncode == 7
        assert "args:one|--flag|two" in result.stdout
        assert "err-line" in result.stderr

    def test_script_own_argparse_help_reachable(self, tmp_path):
        body = (
            "import argparse\n"
            "p = argparse.ArgumentParser(description='Fixture add command.')\n"
            "p.add_argument('--due')\n"
            "p.parse_args()\n"
        )
        ext_dir = tmp_path / "extensions" / "tasks"
        make_command(ext_dir, "add", docstring="Add a task.", body=body)

        result = run_cli(["tasks", "add", "--help"], tmp_path)
        assert result.returncode == 0
        assert "Fixture add command." in result.stdout
        assert "--due" in result.stdout

    def test_unknown_extension_descriptive_error(self, tmp_path):
        result = run_cli(["bogus", "cmd"], tmp_path)
        assert result.returncode == 2
        assert "Unknown command: 'bogus'" in result.stderr

    def test_uv_script_shebang_runs(self, tmp_path):
        """Verify-first assumption: '#!/usr/bin/env -S uv run --script' works."""
        ext_dir = tmp_path / "extensions" / "tasks"
        commands_dir = ext_dir / "commands"
        commands_dir.mkdir(parents=True)
        file = commands_dir / "pep.py"
        file.write_text(PEP723_SCRIPT)
        file.chmod(0o755)

        result = run_cli(["tasks", "pep", "x"], tmp_path)
        assert result.returncode == 0, result.stderr
        assert "pep723-ok:x" in result.stdout


class TestBuiltinNotesEndToEnd:
    """The notes built-in ships real convention commands — exercise them."""

    def _make_kb(self, home: Path) -> None:
        kb = home / "notes" / "kb"
        kb.mkdir(parents=True)
        (kb / "docker-tips.md").write_text(
            "---\n"
            "title: Docker Tips\n"
            "created: 2026-01-01\n"
            "tags: [devops]\n"
            "summary: Compose patterns\n"
            "---\n\n# Docker Tips\n"
        )

    def test_merlin_notes_search_kb(self, tmp_path):
        self._make_kb(tmp_path)
        result = run_cli(["notes", "search", "kb"], tmp_path)
        assert result.returncode == 0, result.stderr
        assert "Docker Tips" in result.stdout

    def test_merlin_kb_alias_add_dry_run(self, tmp_path):
        self._make_kb(tmp_path)
        result = run_cli(
            [
                "kb",
                "add",
                "--type",
                "reference",
                "--title",
                "New Note",
                "--tags",
                "devops",
                "--content",
                "Some content",
                "--dry-run",
            ],
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert "Dry run" in result.stdout
        assert "new-note.md" in result.stdout

    def test_merlin_remember_help(self, tmp_path):
        result = run_cli(["remember", "--help"], tmp_path)
        assert result.returncode == 0, result.stderr
        assert "merlin remember" in result.stdout


class TestEnabledExtensionSourceDirs:
    """Setup-side enabled resolution mirrors the server's."""

    def _write_state(self, state: dict):
        import json

        path = paths.extensions_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))

    def test_builtin_defaults_applied(self, tmp_path):
        # No state file: notes enabled by default, merlin-bot disabled
        sources = ext_commands.enabled_extension_source_dirs()
        assert "notes" in sources
        assert "merlin-bot" not in sources

    def test_explicit_state_wins(self, tmp_path):
        self._write_state({"merlin-bot": True, "notes": False})
        sources = ext_commands.enabled_extension_source_dirs()
        assert "merlin-bot" in sources
        assert "notes" not in sources

    def test_installed_default_enabled_and_state_disable(self, tmp_path):
        make_command(paths.extensions_dir() / "tasks", "add")
        sources = ext_commands.enabled_extension_source_dirs()
        assert "tasks" in sources

        self._write_state({"tasks": False})
        sources = ext_commands.enabled_extension_source_dirs()
        assert "tasks" not in sources

    def test_all_extension_states_reports_enabled_flag(self, tmp_path):
        # all_extension_states lists every extension with its enabled flag,
        # including disabled ones (which enabled_extension_source_dirs drops).
        make_command(paths.extensions_dir() / "tasks", "add")
        self._write_state({"merlin-bot": True, "tasks": False})

        states = ext_commands.all_extension_states()
        assert states["notes"][1] is True  # built-in default
        assert states["merlin-bot"][1] is True  # explicitly enabled
        assert states["tasks"][1] is False  # explicitly disabled, still listed

        # The enabled subset matches enabled_extension_source_dirs exactly.
        enabled = {k for k, (_d, on) in states.items() if on}
        assert enabled == set(ext_commands.enabled_extension_source_dirs())

    def test_all_extension_states_excludes_reserved_installed(self, tmp_path):
        make_command(paths.extensions_dir() / "job", "evil")
        assert "job" not in ext_commands.all_extension_states()

    def test_reserved_installed_excluded(self, tmp_path):
        make_command(paths.extensions_dir() / "job", "evil")
        sources = ext_commands.enabled_extension_source_dirs()
        assert "job" not in sources

    def test_defaults_shared_with_main(self):
        import main

        assert main.BUILT_IN_DEFAULTS is ext_commands.BUILTIN_DEFAULT_ENABLED

    def test_setup_respects_disabled_bot(self, tmp_path, monkeypatch, capsys):
        """merlin setup must not re-expose a disabled extension's skills."""
        from tests.unit.test_cli import _real_refresh_skills

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        # merlin-bot disabled by default -> its skills must not aggregate
        _real_refresh_skills()
        from lib import skills

        assert not (skills.canonical_dir() / "discord").exists()
        assert not (home / ".claude" / "skills" / "discord").exists()

        # Enable it -> skills appear on the next refresh
        self._write_state({"merlin-bot": True})
        _real_refresh_skills()
        assert (skills.canonical_dir() / "discord").is_symlink()
