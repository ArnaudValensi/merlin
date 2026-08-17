"""Activity hook integration against a private tmux server."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


HOOK = Path(__file__).parents[2] / "timeline" / "hooks" / "activity_hook.py"
pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


class PrivateTmux:
    def __init__(self, socket: Path, home: Path):
        self.socket = str(socket)
        self.home = str(home)

    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "HOME": self.home,
            "TERM": "xterm-256color",
            "PATH": os.environ["PATH"],
        }
        return subprocess.run(
            ["tmux", "-S", self.socket, "-f", "/dev/null", *args],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def value(self, target: str, format_value: str) -> str:
        return self.command(
            "display-message", "-p", "-t", target, format_value
        ).stdout.rstrip("\n")

    @property
    def tmux_env(self) -> str:
        pid = self.command("display-message", "-p", "#{pid}").stdout.strip()
        return f"{self.socket},{pid},0"


@pytest.fixture
def private_tmux(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    server = PrivateTmux(tmp_path / "timeline.sock", home)
    server.command("kill-server")
    server.command(
        "new-session", "-d", "-s", "activity", "-c", str(tmp_path), "-n", "agent"
    )
    pane = server.value("activity:agent", "#{pane_id}")
    yield server, pane
    server.command("kill-server")


def run_hook(
    server: PrivateTmux | None,
    pane: str | None,
    home: Path,
    provider: str,
    payload: dict | str,
    capture_mode: str = "auto",
):
    environment = {
        "HOME": str(home),
        "MERLIN_HOME": str(home / ".merlin"),
        "MERLIN_ACTIVITY_HOOKS": capture_mode,
        "PATH": os.environ["PATH"],
    }
    if server is not None:
        environment["TMUX"] = server.tmux_env
    if pane is not None:
        environment["TMUX_PANE"] = pane
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK), provider],
        input=body,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
        check=False,
    )


def records(home: Path) -> list[dict]:
    files = list((home / ".merlin" / "logs" / "activity").glob("*.jsonl"))
    return [
        json.loads(line) for path in files for line in path.read_text().splitlines()
    ]


def test_prompt_stop_flow_ignores_tools_and_uses_stable_tmux_identity(
    private_tmux, tmp_path
):
    server, pane = private_tmux
    server.command("set-option", "-w", "-t", pane, "@agent_state", "ask")
    server.command("set-option", "-w", "-t", pane, "@agent_sid", "core-agent")
    payloads = [
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s",
            "turn_id": "t",
            "prompt": "never store",
        },
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s",
            "turn_id": "t",
            "tool_use_id": "x",
            "tool_name": "exec_command",
            "tool_input": {"cmd": "secret"},
        },
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s",
            "turn_id": "t",
            "tool_use_id": "x",
            "tool_name": "exec_command",
            "tool_response": {"output": "secret"},
        },
        {
            "hook_event_name": "Stop",
            "session_id": "s",
            "turn_id": "t",
            "last_assistant_message": "secret",
        },
    ]
    for payload in payloads:
        result = run_hook(server, pane, tmp_path, "codex", payload)
        assert result.returncode == 0
        assert result.stdout == result.stderr == ""
    output = records(tmp_path)
    assert [item["kind"] for item in output] == [
        "human.prompt",
        "agent.turn",
        "agent.turn",
    ]
    assert len({item["context"]["agent_sid"] for item in output}) == 1
    assert output[0]["context"]["agent_sid"] == "core-agent"
    assert server.value(pane, "#{@agent_sid}") == "core-agent"
    assert server.value(pane, "#{@agent_state}") == "ask"
    serialized = json.dumps(output)
    assert "never store" not in serialized
    assert "secret" not in serialized


def test_resume_keeps_identity_after_window_rename(private_tmux, tmp_path):
    server, pane = private_tmux
    server.command("set-option", "-w", "-t", pane, "@agent_sid", "core-agent")
    start = {"hook_event_name": "SessionStart", "session_id": "s", "source": "startup"}
    resume = {"hook_event_name": "SessionStart", "session_id": "s", "source": "resume"}
    run_hook(server, pane, tmp_path, "codex", start)
    sid = server.value(pane, "#{@agent_sid}")
    server.command("rename-window", "-t", pane, "renamed")
    run_hook(server, pane, tmp_path, "codex", resume)
    output = records(tmp_path)
    assert output[0]["context"]["agent_sid"] == output[1]["context"]["agent_sid"] == sid
    assert output[1]["actor"]["label"].endswith("renamed")


def test_generic_chain_environment_supplies_role_and_profile(
    private_tmux, tmp_path, monkeypatch
):
    server, pane = private_tmux
    monkeypatch.setenv("MERLIN_TIMELINE_ROLE", "Reviewer")
    monkeypatch.setenv("MERLIN_PROVIDER", "codex")
    monkeypatch.setenv("MERLIN_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("MERLIN_EFFORT", "xhigh")
    from timeline import hook_runtime

    monkeypatch.setenv("TMUX", server.tmux_env)
    monkeypatch.setenv("TMUX_PANE", pane)
    value = hook_runtime.tmux_metadata({})
    assert value is not None
    assert value["role"] == "Reviewer"
    assert value["provider"] == "codex"
    assert value["model"] == "gpt-5.6-sol"
    assert value["effort"] == "xhigh"


def test_compaction_malformed_dead_and_outside_tmux_fail_open(private_tmux, tmp_path):
    server, pane = private_tmux
    compact = {
        "hook_event_name": "SessionStart",
        "session_id": "s",
        "source": "compact",
    }
    assert run_hook(server, pane, tmp_path, "codex", compact).returncode == 0
    assert run_hook(server, pane, tmp_path, "codex", "{broken").returncode == 0
    assert run_hook(None, None, tmp_path, "codex", compact).returncode == 0
    server.command("kill-window", "-t", pane)
    assert run_hook(server, pane, tmp_path, "codex", compact).returncode == 0
    assert records(tmp_path) == []


def test_hook_rechecks_capture_consent_before_writing(private_tmux, tmp_path):
    server, pane = private_tmux
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-1",
        "turn_id": "turn-1",
    }

    result = run_hook(server, pane, tmp_path, "codex", payload, "off")

    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert records(tmp_path) == []


def test_missing_core_sid_uses_stable_fallback_without_mutating_tmux(monkeypatch):
    from timeline import hook_runtime

    monkeypatch.setenv("TMUX_PANE", "%4")
    initial = "\x1f".join(
        (
            "work",
            "@2",
            "%4",
            "",
            "/workspace/project",
            "/workspace/project",
            "Agent",
            "",
            "",
            "",
            "",
        )
    )
    calls = []

    def tmux(*args):
        calls.append(args)
        return subprocess.CompletedProcess([], 0, stdout=initial + "\n")

    monkeypatch.setattr(hook_runtime, "_tmux", tmux)

    first = hook_runtime.tmux_metadata({"session_id": "provider-session"})
    second = hook_runtime.tmux_metadata({"session_id": "provider-session"})

    assert first is not None
    assert first["agent_sid"] is None
    assert first["timeline_actor_id"] == second["timeline_actor_id"]
    assert first["timeline_actor_id"].startswith("timeline:")
    assert len(calls) == 2
    assert not any("set-option" in args for args in calls)


def test_timeline_never_pins_core_owned_agent_cwd(monkeypatch):
    from timeline import hook_runtime

    monkeypatch.setenv("TMUX_PANE", "%4")
    initial = "\x1f".join(
        (
            "work",
            "@2",
            "%4",
            "agent-stable",
            "",
            "/workspace/current-subdir",
            "Agent",
            "",
            "",
            "",
            "",
        )
    )
    calls = []

    def tmux(*args):
        calls.append(args)
        return subprocess.CompletedProcess([], 0, stdout=initial + "\n")

    monkeypatch.setattr(hook_runtime, "_tmux", tmux)

    value = hook_runtime.tmux_metadata({})

    assert value is not None
    assert value["cwd"] == "/workspace/current-subdir"
    assert not any("@agent_cwd" in args for args in calls)


def test_normalization_failure_is_counted_without_failing_provider(
    monkeypatch, tmp_path
):
    from timeline import hook_runtime
    from timeline.writer import read_capture_health

    monkeypatch.setattr(hook_runtime, "capture_mode", lambda: "auto")
    monkeypatch.setattr(
        hook_runtime,
        "tmux_metadata",
        lambda _payload: {"agent_sid": "agent-a", "window_name": "Agent"},
    )
    monkeypatch.setattr(
        hook_runtime,
        "normalize_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("unsafe payload")),
    )

    assert hook_runtime.process_payload("codex", {}, directory=tmp_path) == 0
    health = read_capture_health(tmp_path)
    assert health["dropped"] == 1
    assert health["last_error"] == "normalization-error"


def test_hook_entrypoint_has_a_hard_fail_open_deadline(monkeypatch):
    from timeline.hooks import activity_hook

    monkeypatch.setattr(activity_hook, "HOOK_DEADLINE_SECONDS", 0.02)
    monkeypatch.setattr(activity_hook, "read_stdin_payload", lambda: {})
    monkeypatch.setattr(activity_hook, "process_payload", lambda *_args: time.sleep(1))
    monkeypatch.setattr(sys, "argv", ["activity_hook.py", "codex"])
    started = time.monotonic()

    assert activity_hook.main() == 0
    assert time.monotonic() - started < 0.5
