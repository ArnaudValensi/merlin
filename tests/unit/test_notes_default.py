"""Notes is a built-in extension that is off by default.

No state file: no nav item, no routes, no skill, and ``merlin notes``,
``merlin kb``, ``merlin remember`` say the extension is disabled and how to
enable it. An explicit choice in extensions.json still wins, and the
extension creates its own directory when it starts. The suite imports main
with notes enabled (tests/conftest.py seeds the state), so the enabled
behaviour is what every notes suite already proves.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

import ext_commands
import main as app_mod
import paths


def _write_state(state: dict) -> None:
    path = paths.extensions_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


class TestDefault:
    def test_off_without_a_state_file(self):
        assert ext_commands.BUILTIN_DEFAULT_ENABLED["notes"] is False
        assert app_mod._resolve_enabled("notes", "built-in", {}) is False
        assert ext_commands.all_extension_states()["notes"][1] is False
        assert "notes" not in ext_commands.enabled_extension_source_dirs()

    def test_an_explicit_choice_wins(self):
        _write_state({"notes": True})
        assert ext_commands.all_extension_states()["notes"][1] is True
        assert "notes" in ext_commands.enabled_extension_source_dirs()
        assert app_mod._resolve_enabled("notes", "built-in", {"notes": True}) is True

    def test_the_skill_follows_the_extension(self):
        """merlin-notes ships under notes/skills/, so a skill rebuild
        without the extension carries no notes skill, and one with it does."""
        from lib import skills

        registry = skills.build_registry(ext_commands.enabled_extension_source_dirs())
        assert "merlin-notes" not in registry
        _write_state({"notes": True})
        registry = skills.build_registry(ext_commands.enabled_extension_source_dirs())
        assert registry["merlin-notes"].source == "notes"
        assert registry["merlin-notes"].path == (
            paths.app_dir() / "notes" / "skills" / "merlin-notes"
        )


class TestDisabledCommands:
    @pytest.mark.parametrize(
        "argv",
        [
            ["notes", "search", "kb"],
            ["notes", "kb", "index"],
            ["notes", "remember", "list"],
        ],
    )
    def test_say_disabled_and_how_to_enable(self, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            ext_commands.dispatch(argv)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "notes extension is disabled" in err
        assert "/extensions" in err
        assert str(paths.extensions_state_path()) in err

    def test_the_aliases_say_the_same(self, capsys):
        from cli import cli_main

        for alias in ("kb", "remember"):
            with pytest.raises(SystemExit) as exc:
                cli_main([alias, "list"])
            assert exc.value.code == 2
            assert "notes extension is disabled" in capsys.readouterr().err

    def test_only_notes_commands_follow_the_enabled_state(self):
        """The Timeline's capture consent stays reachable with the extension
        off, and installed extensions keep their commands: the gate is the
        notes extension's."""
        assert ext_commands.COMMANDS_FOLLOW_ENABLED == {"notes"}

    def test_enabled_dispatch_reaches_the_command(self, monkeypatch):
        _write_state({"notes": True})
        execs: list[list[str]] = []

        def fake_execv(file, args):
            execs.append(args)
            raise SystemExit(0)

        monkeypatch.setattr(os, "execv", fake_execv)
        with pytest.raises(SystemExit) as exc:
            ext_commands.dispatch(["notes", "search", "kb"])
        assert exc.value.code == 0
        assert execs and execs[0][0].endswith("notes/commands/search.py")
        assert execs[0][1:] == ["kb"]


class TestDirectory:
    def test_the_extension_creates_its_directory_on_start(self, monkeypatch):
        import notes

        async def no_sync(_dir):
            return None

        monkeypatch.setattr(notes, "start_sync", no_sync)
        target = paths.notes_dir()
        assert not target.exists()
        asyncio.run(notes.start())
        assert target.is_dir()

    def test_disabled_notes_leave_the_directory_alone(self):
        """Nothing in the core creates or removes the notes directory: with
        the extension off, an existing directory stays untouched and a
        missing one is not created."""
        target = paths.notes_dir()
        target.mkdir(parents=True)
        (target / "user.md").write_text("facts\n")
        assert ext_commands.all_extension_states()["notes"][1] is False
        assert (target / "user.md").read_text() == "facts\n"
