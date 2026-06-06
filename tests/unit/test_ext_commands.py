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
        for name in ("notes", "merlin-bot", "cron", "files", "terminal", "commits"):
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

    def test_empty_when_no_commands(self):
        assert ext_commands.format_extension_help() == ""

    def test_reserved_installed_extension_skipped_with_warning(
        self, tmp_path, capsys, monkeypatch
    ):
        # Builtin dirs resolve outside tmp MERLIN_HOME — point them elsewhere
        monkeypatch.setattr(ext_commands, "builtin_extension_dirs", lambda: {})
        make_command(paths.extensions_dir() / "cron", "evil")
        text = ext_commands.format_extension_help()
        err = capsys.readouterr().err
        assert "merlin cron evil" not in text
        assert "reserved" in err
        assert "cron" in err

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
        make_command(paths.extensions_dir() / "cron", "evil")
        with pytest.raises(SystemExit) as exc_info:
            ext_commands.dispatch(["cron", "evil"])
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
