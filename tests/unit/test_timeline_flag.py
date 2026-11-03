"""The Timeline does not exist unless MERLIN_FEATURES turns it on.

Flag off: no registry entry, no nav item, no page, no CLI namespace, no help
entry, and the provider hooks a previous version installed are removed at
startup, only when they are there, without loading the extension; recorded
data is kept. Flag on: as today (the suite imports main with the flag on,
so the timeline suites prove that state). The ``timeline`` id stays
reserved either way.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ext_commands
import features
import main as app_mod
import paths
from timeline import reconcile

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv("MERLIN_FEATURES", raising=False)


@pytest.fixture
def hook_files(tmp_path, monkeypatch):
    claude = tmp_path / "claude" / "settings.json"
    codex = tmp_path / "codex" / "hooks.json"
    monkeypatch.setattr(reconcile, "claude_settings_path", lambda: claude)
    monkeypatch.setattr(reconcile, "codex_hooks_path", lambda: codex)
    return claude, codex


def _owned(provider: str, matcher: str | None = None) -> dict:
    return reconcile._group(provider, matcher)


FOREIGN = {"hooks": [{"type": "command", "command": "echo mine"}]}


def _seed(path: Path, groups: dict[str, list[dict]], **extra) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": groups, **extra}, indent=2))


class TestCliSurfaces:
    def test_flag_off_hides_everything(self, flag_off):
        assert not features.enabled("timeline")
        assert "timeline" not in ext_commands.builtin_extension_dirs()
        assert "timeline" not in ext_commands.all_extension_states()
        assert "timeline" not in ext_commands.enabled_extension_source_dirs()
        assert "merlin timeline" not in ext_commands.format_extension_help()

    def test_flag_off_dispatch_is_unknown_command(self, flag_off, capsys):
        with pytest.raises(SystemExit) as exc:
            ext_commands.dispatch(["timeline", "emit"])
        assert exc.value.code == 2
        assert "Unknown command: 'timeline'" in capsys.readouterr().err

    def test_flag_on_exposes_the_extension(self):
        dirs = ext_commands.builtin_extension_dirs()
        assert dirs["timeline"] == paths.app_dir() / "timeline"
        assert ext_commands.all_extension_states()["timeline"][1] is True
        assert "timeline" in ext_commands.enabled_extension_source_dirs()
        assert "merlin timeline" in ext_commands.format_extension_help()

    def test_flag_on_respects_extensions_toggle(self):
        state = paths.extensions_state_path()
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"timeline": false}')
        assert ext_commands.all_extension_states()["timeline"][1] is False

    @pytest.mark.parametrize("flag", ["", "timeline"])
    def test_name_always_reserved(self, monkeypatch, flag):
        monkeypatch.setenv("MERLIN_FEATURES", flag)
        assert "timeline" in ext_commands.reserved_names()

    def test_flagged_builtins_table(self):
        assert ext_commands.FLAGGED_BUILTINS["timeline"] == "timeline"


class TestServer:
    def test_flag_on_at_import(self):
        info = app_mod.extension_registry["timeline"]
        assert info.loaded and info.tier == "built-in"
        assert any(i.get("url") == "/timeline" for i in app_mod.nav_items)

    def test_flag_off_at_import(self, tmp_path):
        """A fresh process without the flag: no registry entry, no nav item,
        no page route. (The suite's own main was imported with the flag.)"""
        env = {
            k: v for k, v in os.environ.items() if k not in ("MERLIN_FEATURES", "TMUX")
        }
        env["MERLIN_HOME"] = str(tmp_path)
        code = (
            "import json, main\n"
            "print('RESULT ' + json.dumps({"
            "'registry': 'timeline' in main.extension_registry,"
            "'nav': any(i.get('url') == '/timeline' for i in main.nav_items),"
            "'route': any(getattr(r, 'path', '') == '/timeline' for r in main.app.router.routes),"
            "'app': 'app' in main.extension_registry}))\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout
        line = [l for l in out.splitlines() if l.startswith("RESULT ")][-1]
        assert json.loads(line[7:]) == {
            "registry": False,
            "nav": False,
            "route": False,
            "app": False,
        }


class TestHookRetirement:
    """``_retire_timeline_hooks`` runs at server start, off the loop."""

    def test_flag_off_removes_owned_groups_and_keeps_the_rest(
        self, flag_off, hook_files
    ):
        claude, codex = hook_files
        _seed(
            claude,
            {
                "SessionStart": [FOREIGN, _owned("claude")],
                "Stop": [_owned("claude")],
                "PreToolUse": [FOREIGN],
            },
            theme="dark",
        )
        _seed(codex, {"UserPromptSubmit": [_owned("codex")]}, model="o3")
        activity = paths.logs_dir() / "activity"
        activity.mkdir(parents=True)
        (activity / "2026-11-01.jsonl").write_text('{"kept": true}\n')

        assert reconcile.hooks_present()
        assert app_mod._retire_timeline_hooks() is True

        after = json.loads(claude.read_text())
        assert after["theme"] == "dark"
        assert after["hooks"] == {"SessionStart": [FOREIGN], "PreToolUse": [FOREIGN]}
        after_codex = json.loads(codex.read_text())
        assert after_codex == {"model": "o3"}
        assert not reconcile.hooks_present()
        assert (activity / "2026-11-01.jsonl").read_text() == '{"kept": true}\n'

    def test_flag_off_without_hooks_touches_nothing(self, flag_off, hook_files):
        claude, codex = hook_files
        _seed(claude, {"SessionStart": [FOREIGN]}, theme="dark")
        before = claude.read_bytes()
        stat = claude.stat()
        assert not reconcile.hooks_present()
        assert app_mod._retire_timeline_hooks() is False
        assert claude.read_bytes() == before
        assert claude.stat().st_mtime_ns == stat.st_mtime_ns
        assert not codex.exists()

    def test_flag_on_leaves_the_hooks_to_the_extension(self, hook_files):
        claude, _codex = hook_files
        _seed(claude, {"Stop": [_owned("claude")]})
        before = claude.read_bytes()
        assert app_mod._retire_timeline_hooks() is False
        assert claude.read_bytes() == before

    def test_a_second_start_is_a_no_op(self, flag_off, hook_files):
        claude, _codex = hook_files
        _seed(claude, {"Stop": [_owned("claude")]})
        assert app_mod._retire_timeline_hooks() is True
        stat = claude.stat()
        assert app_mod._retire_timeline_hooks() is False
        assert claude.stat().st_mtime_ns == stat.st_mtime_ns

    def test_an_unreadable_file_is_left_alone(self, flag_off, hook_files):
        claude, _codex = hook_files
        claude.parent.mkdir(parents=True)
        claude.write_text("{not json")
        assert not reconcile.hooks_present()
        assert app_mod._retire_timeline_hooks() is False
        assert claude.read_text() == "{not json"

    def test_an_undecodable_file_never_aborts_startup(self, flag_off, hook_files):
        """One provider file is not UTF-8: it is left untouched, the other
        provider is still retired, and nothing raises into the startup."""
        claude, codex = hook_files
        claude.parent.mkdir(parents=True)
        claude.write_bytes(b'{"hooks": \xff}')
        _seed(codex, {"Stop": [_owned("codex")]}, model="o3")
        assert reconcile.hooks_present()
        assert app_mod._retire_timeline_hooks() is True
        assert claude.read_bytes() == b'{"hooks": \xff}'
        assert json.loads(codex.read_text()) == {"model": "o3"}
        # And with nothing left to retire, the undecodable file alone is a
        # quiet no-op, not an exception.
        assert app_mod._retire_timeline_hooks() is False

    def test_a_provider_without_owned_groups_is_kept_byte_for_byte(
        self, flag_off, hook_files
    ):
        """Presence is decided per provider under its lock: the file with
        an owned group is rewritten, the other is not, even when a removal
        would have tidied an empty event list."""
        claude, codex = hook_files
        _seed(claude, {"Stop": [_owned("claude")]})
        codex.parent.mkdir(parents=True)
        codex.write_text('{"model": "user-choice", "hooks": {"Stop": []}}')
        before = codex.read_bytes()
        stat = codex.stat()
        assert app_mod._retire_timeline_hooks() is True
        assert "hooks" not in json.loads(claude.read_text())
        assert codex.read_bytes() == before
        assert codex.stat().st_mtime_ns == stat.st_mtime_ns

    def test_an_exception_in_retirement_is_logged_not_raised(
        self, flag_off, hook_files, monkeypatch, caplog
    ):
        _seed(hook_files[0], {"Stop": [_owned("claude")]})

        def boom():
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(reconcile, "remove_hooks", boom)
        assert app_mod._retire_timeline_hooks() is False
        assert "could not retire" in caplog.text

    def test_hooks_present_is_a_read(self, hook_files):
        claude, codex = hook_files
        assert reconcile.hooks_present() is False
        assert not claude.exists() and not codex.exists()
        _seed(codex, {"Stop": [_owned("codex")]})
        before = codex.read_bytes()
        assert reconcile.hooks_present() is True
        assert codex.read_bytes() == before
