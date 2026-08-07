"""Tests for the agent-state hooks config + settings.json reconciler.

Covers the consent config (auto|ask|off), the collision-safe atomic merge of
Merlin's three Claude Code hooks into ~/.claude/settings.json, drift detection,
idempotency, never-clobbers-foreign, clean removal on `off`, and the mode
dispatch of sync_interactive_hooks().
"""

import json
import subprocess

import paths
import pytest

from lib import skills


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Redirect claude_settings_path() to a temp file (never touch ~/.claude)."""
    p = tmp_path / "dot-claude" / "settings.json"
    monkeypatch.setattr(skills, "claude_settings_path", lambda: p)
    return p


def read(p):
    return json.loads(p.read_text())


def merlin_commands(settings: dict) -> list[str]:
    out = []
    for groups in settings.get("hooks", {}).values():
        for g in groups:
            for h in g.get("hooks", []):
                if skills._HOOK_MARKER in h.get("command", ""):
                    out.append(h["command"])
    return out


# ---------------------------------------------------------------------------
# Consent config (auto|ask|off)
# ---------------------------------------------------------------------------
class TestConsentConfig:
    def test_default_is_ask(self, monkeypatch):
        monkeypatch.delenv("AGENT_STATE_HOOKS", raising=False)
        assert skills.agent_state_hooks_mode() == "ask"

    def test_set_and_read_auto(self):
        assert skills.set_agent_state_hooks_mode("auto") == "auto"
        assert skills.agent_state_hooks_mode() == "auto"

    def test_set_off(self):
        skills.set_agent_state_hooks_mode("off")
        assert skills.agent_state_hooks_mode() == "off"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            skills.set_agent_state_hooks_mode("bogus")

    def test_normalizes_case_and_whitespace(self):
        assert skills.set_agent_state_hooks_mode("  AUTO ") == "auto"

    def test_persists_to_config_env(self):
        skills.set_agent_state_hooks_mode("off")
        text = paths.config_path().read_text()
        assert "AGENT_STATE_HOOKS=off" in text

    def test_unknown_stored_value_falls_back_to_ask(self):
        skills._config_env_write("AGENT_STATE_HOOKS", "weird")
        assert skills.agent_state_hooks_mode() == "ask"


# ---------------------------------------------------------------------------
# install_interactive_hooks
# ---------------------------------------------------------------------------
class TestInstall:
    def test_creates_three_events_in_empty(self, settings_file):
        assert skills.install_interactive_hooks() is True
        data = read(settings_file)
        assert set(data["hooks"]) == {"UserPromptSubmit", "Stop", "SessionStart"}
        # exactly one Merlin command per event, with the right state
        cmds = {ev: g[0]["hooks"][0]["command"] for ev, g in data["hooks"].items()}
        assert " busy " in cmds["UserPromptSubmit"]
        assert " done " in cmds["Stop"]
        assert " idle " in cmds["SessionStart"]
        for c in cmds.values():
            assert skills._HOOK_MARKER in c

    def test_is_idempotent(self, settings_file):
        assert skills.install_interactive_hooks() is True
        before = settings_file.read_text()
        assert skills.install_interactive_hooks() is False  # no drift, no write
        assert settings_file.read_text() == before

    def test_preserves_foreign_keys_and_hooks(self, settings_file):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "theme": "dark-daltonized",
                    "hooks": {
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "echo mine"}]}
                        ],
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "guard.sh"}],
                            }
                        ],
                    },
                }
            )
        )
        assert skills.install_interactive_hooks() is True
        data = read(settings_file)
        # unrelated top-level keys survive
        assert data["model"] == "opus"
        assert data["theme"] == "dark-daltonized"
        # the user's own Stop hook is still there, alongside Merlin's
        stop_cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        assert "echo mine" in stop_cmds
        assert any(skills._HOOK_MARKER in c for c in stop_cmds)
        # a foreign event Merlin doesn't manage is untouched
        assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "guard.sh"

    def test_never_duplicates_foreign_stop(self, settings_file):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "echo mine"}]}
                        ]
                    }
                }
            )
        )
        skills.install_interactive_hooks()
        skills.install_interactive_hooks()  # twice
        data = read(settings_file)
        stop_cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        assert stop_cmds.count("echo mine") == 1  # foreign not duplicated
        assert sum(skills._HOOK_MARKER in c for c in stop_cmds) == 1  # nor Merlin's

    def test_reinstalls_on_version_drift(self, settings_file, monkeypatch):
        cur = skills._HOOK_VERSION
        nxt = cur + 1
        skills.install_interactive_hooks()
        old_cmds = merlin_commands(read(settings_file))
        assert all(f":v{cur}" in c for c in old_cmds)
        monkeypatch.setattr(skills, "_HOOK_VERSION", nxt)
        assert skills.interactive_hooks_drift() is True
        assert skills.install_interactive_hooks() is True
        new_cmds = merlin_commands(read(settings_file))
        assert new_cmds and all(f":v{nxt}" in c for c in new_cmds)
        assert not any(f":v{cur}" in c for c in new_cmds)  # old marker gone, no dupes

    def test_session_start_also_runs_board_session_init(self, settings_file):
        # SessionStart carries two Merlin commands: the idle pill setter AND the
        # board session-init (mints @agent_sid, pins @agent_cwd).
        skills.install_interactive_hooks()
        data = read(settings_file)
        start_cmds = [
            h["command"]
            for g in data["hooks"]["SessionStart"]
            for h in g["hooks"]
            if skills._HOOK_MARKER in h.get("command", "")
        ]
        assert any("agent-state.sh" in c and " idle " in c for c in start_cmds)
        assert any("agent-session-init.sh" in c for c in start_cmds)
        # The other two events stay single-command.
        for ev in ("UserPromptSubmit", "Stop"):
            ev_cmds = [
                h["command"]
                for g in data["hooks"][ev]
                for h in g["hooks"]
                if skills._HOOK_MARKER in h.get("command", "")
            ]
            assert len(ev_cmds) == 1
            assert "agent-session-init.sh" not in ev_cmds[0]

    def test_bails_on_invalid_json(self, settings_file):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("{ this is not json")
        assert skills.install_interactive_hooks() is False
        assert settings_file.read_text() == "{ this is not json"  # untouched

    def test_bails_on_bad_hooks_shape(self, settings_file):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({"hooks": ["not", "a", "dict"]}))
        assert skills.install_interactive_hooks() is False

    def test_output_is_valid_json_with_trailing_newline(self, settings_file):
        skills.install_interactive_hooks()
        raw = settings_file.read_text()
        assert raw.endswith("\n")
        json.loads(raw)  # parses


# ---------------------------------------------------------------------------
# remove_interactive_hooks
# ---------------------------------------------------------------------------
class TestRemove:
    def test_removes_and_drops_empty_hooks(self, settings_file):
        skills.install_interactive_hooks()
        assert skills.remove_interactive_hooks() is True
        data = read(settings_file)
        assert "hooks" not in data  # nothing else was there, so hooks is gone

    def test_keeps_foreign_hooks(self, settings_file):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "echo mine"}]}
                        ]
                    },
                }
            )
        )
        skills.install_interactive_hooks()
        skills.remove_interactive_hooks()
        data = read(settings_file)
        stop_cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
        assert stop_cmds == ["echo mine"]  # foreign kept, Merlin gone
        assert data["model"] == "opus"

    def test_noop_when_absent(self, settings_file):
        assert skills.remove_interactive_hooks() is False

    def test_noop_when_only_foreign(self, settings_file):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}
        )
        settings_file.write_text(original)
        assert skills.remove_interactive_hooks() is False
        assert settings_file.read_text() == original


# ---------------------------------------------------------------------------
# drift + sync dispatch
# ---------------------------------------------------------------------------
class TestDriftAndSync:
    def test_drift_true_when_not_installed(self, settings_file):
        assert skills.interactive_hooks_drift() is True

    def test_drift_false_when_installed(self, settings_file):
        skills.install_interactive_hooks()
        assert skills.interactive_hooks_drift() is False

    def test_drift_false_on_unreadable(self, settings_file):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("{ broken")
        assert skills.interactive_hooks_drift() is False

    def test_sync_auto_installs(self, settings_file):
        skills.set_agent_state_hooks_mode("auto")
        assert skills.sync_interactive_hooks() == "synced"
        assert settings_file.exists()
        assert skills.sync_interactive_hooks() == "in-sync"  # second is a no-op

    def test_sync_off_removes(self, settings_file):
        skills.install_interactive_hooks()
        skills.set_agent_state_hooks_mode("off")
        assert skills.sync_interactive_hooks() == "removed"
        assert skills.sync_interactive_hooks() == "clean"

    def test_sync_ask_does_not_write_but_reports_pending(self, settings_file):
        skills.set_agent_state_hooks_mode("ask")
        assert skills.sync_interactive_hooks() == "pending"
        assert not settings_file.exists()  # ask never writes at startup

    def test_sync_ask_in_sync_when_already_installed(self, settings_file):
        skills.install_interactive_hooks()  # e.g. installed earlier under auto
        skills.set_agent_state_hooks_mode("ask")
        assert skills.sync_interactive_hooks() == "in-sync"


# ---------------------------------------------------------------------------
# The version marker is a harmless trailing shell comment
# ---------------------------------------------------------------------------
class TestMarkerComment:
    def test_comment_does_not_reach_script_args(self, tmp_path):
        script = tmp_path / "echo.sh"
        script.write_text('#!/bin/bash\nprintf "[%s]" "$@"\n')
        script.chmod(0o755)
        cmd = f'bash "{script}" busy  # {skills._HOOK_MARKER}:v1'
        r = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True, check=False
        )
        assert r.stdout == "[busy]"  # only the state, comment stripped by shell
