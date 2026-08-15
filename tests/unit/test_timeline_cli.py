"""Public Timeline extension command behavior and privacy tests."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone

import pytest

from timeline.commands import capture, emit
from timeline.store import ActivityStore


def test_command_is_discoverable_executable_and_documented():
    import ext_commands

    command = ext_commands.list_commands(
        ext_commands.builtin_extension_dirs()["timeline"]
    )["emit"]
    assert command.stat().st_mode & stat.S_IXUSR
    assert ext_commands.extract_help(command).startswith("Emit a private Timeline")
    help_text = emit.build_parser().format_help()
    assert "--point" in help_text
    assert "--strict" in help_text
    assert "dashboard need not be running" in help_text

    capture_command = ext_commands.list_commands(
        ext_commands.builtin_extension_dirs()["timeline"]
    )["capture"]
    assert capture_command.stat().st_mode & stat.S_IXUSR
    assert ext_commands.extract_help(capture_command).startswith(
        "Show or change Timeline"
    )


def test_capture_command_shows_and_changes_independent_consent(monkeypatch, capsys):
    monkeypatch.setattr(capture, "capture_setting", lambda: ("ask", "default"))
    assert capture.main([]) == 0
    assert capsys.readouterr().out == "timeline capture = ask (default)\n"

    calls = []
    monkeypatch.setattr(
        capture, "set_capture_mode", lambda mode: calls.append(mode) or mode
    )
    monkeypatch.setattr(capture, "sync_hooks", lambda: "removed")
    assert capture.main(["off"]) == 0
    assert calls == ["off"]
    assert capsys.readouterr().out == "timeline capture = off (removed)\n"


def test_capture_command_reports_reconciliation_failure(monkeypatch, capsys):
    monkeypatch.setattr(capture, "set_capture_mode", lambda mode: mode)
    monkeypatch.setattr(capture, "sync_hooks", lambda: "error")
    assert capture.main(["auto"]) == 1
    assert capsys.readouterr().out == "timeline capture = auto (error)\n"


def test_capture_command_dispatch_remains_available_while_extension_is_disabled(
    monkeypatch,
):
    import ext_commands

    executed = []

    def intercept(path, argv):
        executed.append((path, argv))
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(
        ext_commands, "_load_extensions_state", lambda: {"timeline": False}
    )
    monkeypatch.setattr(ext_commands.os, "execv", intercept)

    with pytest.raises(RuntimeError, match="exec intercepted"):
        ext_commands.dispatch(["timeline", "capture", "off"])

    assert executed[0][1][-1] == "off"


def test_emit_point_and_span_with_server_down(tmp_path, monkeypatch):
    monkeypatch.setenv("MERLIN_ACTIVITY_HOOKS", "auto")
    monkeypatch.setenv("MERLIN_SERVER", "http://127.0.0.1:1")
    assert (
        emit.main(
            [
                "--kind",
                "review.request",
                "--point",
                "--trace",
                "chain-1",
                "--name",
                "Review requested",
            ]
        )
        == 0
    )
    assert (
        emit.main(
            [
                "--kind",
                "review.await",
                "--start",
                "--trace",
                "chain-1",
                "--span",
                "wait-1",
                "--name",
                "Await reviewer",
            ]
        )
        == 0
    )
    assert (
        emit.main(
            [
                "--kind",
                "review.await",
                "--finish",
                "--trace",
                "chain-1",
                "--span",
                "wait-1",
                "--status",
                "ok",
                "--name",
                "Reviewer signaled",
            ]
        )
        == 0
    )
    result = ActivityStore().read_range(
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    assert [event.phase for event in result.events] == ["point", "start", "finish"]


def test_flags_override_environment_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("MERLIN_ACTIVITY_HOOKS", "auto")
    monkeypatch.setenv("MERLIN_TIMELINE_TRACE_ID", "env-trace")
    monkeypatch.setenv("MERLIN_TIMELINE_ACTOR_ID", "env-actor")
    monkeypatch.setenv("MERLIN_PROVIDER", "env-provider")
    args = emit.build_parser().parse_args(
        [
            "--kind",
            "automation.script",
            "--point",
            "--trace",
            "flag-trace",
            "--name",
            "Targeted checks",
            "--actor-id",
            "flag-actor",
            "--provider",
            "flag-provider",
        ]
    )
    event = emit.event_from_args(args)
    assert event["trace_id"] == "flag-trace"
    assert event["actor"]["id"] == "flag-actor"
    assert event["context"]["provider"] == "flag-provider"


def test_point_ignores_inherited_span_id(tmp_path, monkeypatch):
    monkeypatch.setenv("MERLIN_ACTIVITY_HOOKS", "auto")
    monkeypatch.setenv("MERLIN_TIMELINE_SPAN_ID", "outer-span")

    assert (
        emit.main(
            [
                "--kind",
                "review.request",
                "--point",
                "--trace",
                "chain-1",
                "--name",
                "Review requested",
                "--strict",
            ]
        )
        == 0
    )

    result = ActivityStore().read_range(
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    assert len(result.events) == 1
    assert result.events[0].phase == "point"
    assert result.events[0].span_id is None


def test_disabled_capture_is_silent_unless_strict(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MERLIN_ACTIVITY_HOOKS", "off")
    argv = ["--kind", "review.request", "--point", "--name", "Review"]
    assert emit.main(argv) == 0
    assert not (tmp_path / "logs" / "activity").exists()
    assert emit.main([*argv, "--strict", "--json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "disabled"
    assert payload["ok"] is False


def test_invalid_event_fails_open_or_returns_machine_error(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("MERLIN_ACTIVITY_HOOKS", "auto")
    argv = ["--kind", "review.await", "--start", "--name", "Missing span"]
    assert emit.main(argv) == 0
    assert emit.main([*argv, "--strict", "--json"]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["ok"] is False
    assert "span_id" in output.err


def test_attributes_reject_content_keys_and_do_not_leak(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MERLIN_ACTIVITY_HOOKS", "auto")
    secret = "secret command payload"
    argv = [
        "--kind",
        "tool.call",
        "--point",
        "--name",
        "Shell tool",
        "--attribute",
        f"command={secret}",
        "--strict",
        "--json",
    ]
    assert emit.main(argv) == 2
    output = capsys.readouterr()
    assert secret not in output.out
    assert secret not in output.err
    activity = tmp_path / "logs" / "activity"
    assert not activity.exists() or secret not in "".join(
        path.read_text() for path in activity.glob("*.jsonl")
    )


def test_event_id_is_idempotent_through_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("MERLIN_ACTIVITY_HOOKS", "auto")
    event_id = "00000000-0000-4000-8000-000000000099"
    argv = [
        "--kind",
        "review.request",
        "--point",
        "--name",
        "Review",
        "--event-id",
        event_id,
    ]
    assert emit.main(argv) == 0
    assert emit.main(argv) == 0
    files = list((tmp_path / "logs" / "activity").glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].read_text().count(event_id) == 1


def test_command_does_not_require_server_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MERLIN_ACTIVITY_HOOKS", "auto")
    monkeypatch.delenv("MERLIN_SERVER", raising=False)
    assert (
        emit.main(["--kind", "automation.script", "--point", "--name", "Offline"]) == 0
    )
    assert os.environ.get("MERLIN_SERVER") is None
