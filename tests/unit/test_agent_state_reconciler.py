"""Tests for the agent-state hooks config + Claude/Codex reconcilers.

Covers the consent config (auto|ask|off), the collision-safe atomic merge of
Merlin's hooks into ~/.claude/settings.json and ~/.codex/hooks.json, drift
detection, idempotency, never-clobbers-foreign, clean removal on `off`, and the
mode dispatch of sync_interactive_hooks().
"""

import json
import subprocess

import paths
import pytest

from lib import skills


@pytest.fixture
def hook_files(tmp_path, monkeypatch):
    """Redirect both user hook files so tests never touch the real home."""
    claude = tmp_path / "dot-claude" / "settings.json"
    codex = tmp_path / "dot-codex" / "hooks.json"
    monkeypatch.setattr(skills, "claude_settings_path", lambda: claude)
    monkeypatch.setattr(skills, "codex_hooks_path", lambda: codex)
    return claude, codex


@pytest.fixture
def settings_file(hook_files):
    return hook_files[0]


@pytest.fixture
def codex_file(hook_files):
    return hook_files[1]


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
    def test_creates_every_event_in_empty(self, settings_file):
        assert skills.install_interactive_hooks() is True
        data = read(settings_file)
        assert set(data["hooks"]) == set(skills._CLAUDE_HOOK_EVENTS)
        # exactly one Merlin command per event, with the right state
        cmds = {ev: g[0]["hooks"][0]["command"] for ev, g in data["hooks"].items()}
        assert " busy " in cmds["UserPromptSubmit"]
        assert " done " in cmds["Stop"]
        assert " idle " in cmds["SessionStart"]
        for c in cmds.values():
            assert skills._HOOK_MARKER in c

    def test_ask_state_events_carry_their_matchers(self, settings_file):
        """The dialog hooks are tool-scoped; the permission one is type-scoped.
        A missing matcher would fire them on every tool and stomp 'busy'."""
        skills.install_interactive_hooks()
        hooks = read(settings_file)["hooks"]
        groups = {ev: hooks[ev][0] for ev in hooks}

        assert groups["PreToolUse"]["matcher"] == "AskUserQuestion|ExitPlanMode"
        assert " ask " in groups["PreToolUse"]["hooks"][0]["command"]
        assert groups["PostToolUse"]["matcher"] == "AskUserQuestion|ExitPlanMode"
        assert " busy " in groups["PostToolUse"]["hooks"][0]["command"]
        assert groups["Notification"]["matcher"] == "permission_prompt"
        assert " ask " in groups["Notification"]["hooks"][0]["command"]

    def test_reset_events_are_unmatched(self, settings_file):
        """PostToolBatch must fire for ANY batch: a permission prompt can attach
        to any tool, so the reset cannot be tool-name scoped."""
        skills.install_interactive_hooks()
        hooks = read(settings_file)["hooks"]
        assert "matcher" not in hooks["PostToolBatch"][0]
        assert " busy " in hooks["PostToolBatch"][0]["hooks"][0]["command"]
        for event in ("UserPromptSubmit", "Stop", "SessionStart"):
            assert "matcher" not in hooks[event][0]

    def test_no_unmatched_pretooluse_hook(self, settings_file):
        """Ordering hazard guard: PreToolUse fires BEFORE the permission check
        and Notification/permission_prompt after it, so an unmatched
        PreToolUse -> busy would stomp 'ask' on every permission dialog."""
        skills.install_interactive_hooks()
        merlin_groups = [
            g for g in read(settings_file)["hooks"]["PreToolUse"] if "matcher" in g
        ]
        assert len(merlin_groups) == 1
        assert merlin_groups[0]["matcher"] == "AskUserQuestion|ExitPlanMode"

    def test_upgrade_from_v2_replaces_old_groups(self, settings_file):
        """A user installed at v2 has three unmatched groups. Reconciling must
        strip them by marker and leave exactly one Merlin group per event."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        v2 = f"bash /old/path/agent-state.sh busy  # {skills._HOOK_MARKER}:v2"
        settings_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {"hooks": [{"type": "command", "command": v2}]}
                        ]
                    }
                }
            )
        )
        assert skills.interactive_hooks_drift() is True
        assert skills.install_interactive_hooks() is True
        hooks = read(settings_file)["hooks"]
        assert len(hooks["UserPromptSubmit"]) == 1
        cmd = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
        assert "/old/path" not in cmd
        assert f"{skills._HOOK_MARKER}:v{skills._HOOK_VERSION}" in cmd
        assert set(hooks) == set(skills._CLAUDE_HOOK_EVENTS)

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
        # Claude is skipped, but the independent Codex file is still installed.
        assert skills.install_interactive_hooks() is True
        assert settings_file.read_text() == "{ this is not json"  # untouched

    def test_bails_on_bad_hooks_shape(self, settings_file):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({"hooks": ["not", "a", "dict"]}))
        assert skills.install_interactive_hooks() is True
        assert read(settings_file) == {"hooks": ["not", "a", "dict"]}

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
        # Keep Codex in sync so this assertion isolates the malformed Claude
        # file: unsafe files do not create consent-banner drift.
        skills.install_interactive_hooks()
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
# Codex hooks.json mapping
# ---------------------------------------------------------------------------
class TestCodexInstall:
    def test_creates_native_lifecycle_hooks(self, codex_file):
        assert skills.install_interactive_hooks() is True
        data = read(codex_file)
        assert set(data["hooks"]) == set(skills._CODEX_HOOK_EVENTS)

        groups = {event: data["hooks"][event][0] for event in data["hooks"]}
        commands = {
            event: group["hooks"][0]["command"] for event, group in groups.items()
        }
        assert " busy " in commands["UserPromptSubmit"]
        assert " done " in commands["Stop"]
        assert " idle " in commands["SessionStart"]
        assert " ask " in commands["PermissionRequest"]
        assert " busy " in commands["PostToolUse"]
        assert all(skills._HOOK_MARKER in command for command in commands.values())

    def test_question_and_permission_transitions(self, codex_file):
        skills.install_interactive_hooks()
        groups = {
            event: read(codex_file)["hooks"][event][0]
            for event in skills._CODEX_HOOK_EVENTS
        }

        assert groups["PreToolUse"]["matcher"] == "^request_user_input$"
        assert " ask " in groups["PreToolUse"]["hooks"][0]["command"]
        assert "matcher" not in groups["PermissionRequest"]
        assert " ask " in groups["PermissionRequest"]["hooks"][0]["command"]
        # Any completed tool clears a resolved question/approval and returns
        # the turn to working.
        assert "matcher" not in groups["PostToolUse"]
        assert " busy " in groups["PostToolUse"]["hooks"][0]["command"]

    def test_session_start_excludes_mid_turn_compaction(self, codex_file):
        skills.install_interactive_hooks()
        group = read(codex_file)["hooks"]["SessionStart"][0]
        assert group["matcher"] == "startup|resume|clear"
        commands = [hook["command"] for hook in group["hooks"]]
        assert any("agent-state.sh" in command for command in commands)
        assert any("agent-session-init.sh" in command for command in commands)

    def test_preserves_foreign_codex_hooks(self, codex_file):
        codex_file.parent.mkdir(parents=True, exist_ok=True)
        codex_file.write_text(
            json.dumps(
                {
                    "description": "mine",
                    "hooks": {
                        "Stop": [{"hooks": [{"type": "command", "command": "save.sh"}]}]
                    },
                }
            )
        )
        skills.install_interactive_hooks()
        data = read(codex_file)
        assert data["description"] == "mine"
        stop_commands = [
            hook["command"]
            for group in data["hooks"]["Stop"]
            for hook in group["hooks"]
        ]
        assert "save.sh" in stop_commands
        assert sum(skills._HOOK_MARKER in command for command in stop_commands) == 1

    def test_off_removes_only_merlin_codex_groups(self, codex_file):
        codex_file.parent.mkdir(parents=True, exist_ok=True)
        codex_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [{"hooks": [{"type": "command", "command": "save.sh"}]}]
                    }
                }
            )
        )
        skills.install_interactive_hooks()
        assert skills.remove_interactive_hooks() is True
        assert read(codex_file) == {
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "save.sh"}]}]}
        }


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
