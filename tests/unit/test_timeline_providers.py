"""Provider lifecycle normalization, privacy, and recovery tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from timeline import correlation
from timeline.providers import normalize_payload


FIXTURES = Path(__file__).parents[1] / "fixtures" / "activity_hooks"
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def cases(provider: str) -> dict[str, dict]:
    return {
        item["case"]: item["payload"]
        for item in json.loads((FIXTURES / f"{provider}.json").read_text())
    }


def metadata(agent_sid: str = "agent-fixture") -> dict:
    return {
        "agent_sid": agent_sid,
        "cwd": "/workspace/alpha",
        "project": "alpha",
        "tmux_session": "work",
        "tmux_window": "@7",
        "tmux_pane": "%9",
        "window_name": "Implement",
        "role": "Implementer",
        "model": None,
        "effort": "high",
    }


def test_window_name_is_sanitized_and_bounded():
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "session",
        "source": "startup",
    }
    unsafe = metadata()
    unsafe["window_name"] = "x" * 200 + "\nsecret"

    records = normalize_payload("codex", payload, unsafe)

    assert len(records) == 1
    label = records[0]["actor"]["label"]
    assert "\n" not in label
    assert len(label.encode()) <= 96


def test_private_actor_fallback_does_not_claim_core_liveness_identity(tmp_path):
    value = metadata()
    value["agent_sid"] = None
    value["timeline_actor_id"] = "timeline:fallback"

    records = normalize_payload(
        "codex",
        cases("codex")["prompt_submitted"],
        value,
        now=NOW,
        correlation_dir=tmp_path,
    )

    turn = records[1]
    assert turn["actor"]["id"] == "timeline:fallback"
    assert "agent_sid" not in turn["context"]


def test_unsafe_inherited_trace_falls_back_to_provider_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MERLIN_TIMELINE_TRACE_ID", "unsafe trace+#")

    records = normalize_payload(
        "codex",
        cases("codex")["prompt_submitted"],
        metadata(),
        now=NOW,
        correlation_dir=tmp_path,
    )

    assert len(records) == 2
    assert records[0]["trace_id"].startswith("codex:session:")
    assert records[0]["trace_id"] == records[1]["trace_id"]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_prompt_turn_stop_is_correlated_and_tools_are_ignored(provider, tmp_path):
    fixture = cases(provider)
    output = []
    names = ["prompt_submitted", "tool_start"]
    names.append("tool_failure" if provider == "claude" else "tool_finish")
    names.append("turn_stop")
    for name in names:
        output.extend(
            normalize_payload(
                provider, fixture[name], metadata(), now=NOW, correlation_dir=tmp_path
            )
        )
    assert [event["kind"] for event in output] == [
        "human.prompt",
        "agent.turn",
        "agent.turn",
    ]
    turn = [event for event in output if event["kind"] == "agent.turn"]
    assert turn[0]["span_id"] == turn[1]["span_id"]
    serialized = json.dumps(output).lower()
    assert "<redacted>" not in serialized
    for forbidden_key in (
        "tool_input",
        "tool_response",
        "last_assistant_message",
    ):
        assert f'"{forbidden_key}":' not in serialized


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_tool_permission_and_question_boundaries_are_ignored(provider, tmp_path):
    fixture = cases(provider)
    names = [
        "tool_start",
        "tool_failure" if provider == "claude" else "tool_finish",
        "question_start",
        "question_finish",
        "permission_requested",
    ]
    for name in names:
        assert (
            normalize_payload(
                provider,
                fixture[name],
                metadata(),
                now=NOW,
                correlation_dir=tmp_path,
            )
            == []
        )
    assert not (tmp_path / ".pending.json").exists()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_compaction_session_start_is_not_activity(provider, tmp_path):
    assert (
        normalize_payload(
            provider,
            cases(provider)["session_compact"],
            metadata(),
            now=NOW,
            correlation_dir=tmp_path,
        )
        == []
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_start_and_resume_are_session_points(provider, tmp_path):
    fixture = cases(provider)
    start = normalize_payload(
        provider,
        fixture["session_start_startup"],
        metadata(),
        now=NOW,
        correlation_dir=tmp_path,
    )
    resume = normalize_payload(
        provider,
        fixture["session_resume"],
        metadata(),
        now=NOW,
        correlation_dir=tmp_path,
    )
    assert start[0]["kind"] == resume[0]["kind"] == "session.lifecycle"
    assert start[0]["trace_id"] == resume[0]["trace_id"]
    assert start[0]["name"] == "Session started"
    assert resume[0]["name"] == "Session resumed"


def test_missing_ids_never_join_different_agents(tmp_path):
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "same"}
    one = normalize_payload(
        "codex", payload, metadata("agent-one"), now=NOW, correlation_dir=tmp_path
    )
    two = normalize_payload(
        "codex", payload, metadata("agent-two"), now=NOW, correlation_dir=tmp_path
    )
    assert one[1]["span_id"] != two[1]["span_id"]
    stop = normalize_payload(
        "codex",
        {"hook_event_name": "Stop", "session_id": "same"},
        metadata("agent-one"),
        now=NOW,
        correlation_dir=tmp_path,
    )
    assert stop[0]["span_id"] == one[1]["span_id"]


def test_queued_missing_id_prompt_does_not_duplicate_turn_start(tmp_path):
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "same"}

    first = normalize_payload(
        "codex", payload, metadata(), now=NOW, correlation_dir=tmp_path
    )
    queued = normalize_payload(
        "codex", payload, metadata(), now=NOW, correlation_dir=tmp_path
    )

    assert [record["kind"] for record in first] == ["human.prompt", "agent.turn"]
    assert [record["kind"] for record in queued] == ["human.prompt"]


def test_missing_tool_ids_are_ignored_without_correlation_state(tmp_path):
    start = {
        "hook_event_name": "PreToolUse",
        "session_id": "s",
        "tool_name": "exec_command",
    }
    finish = {**start, "hook_event_name": "PostToolUse"}

    first = normalize_payload(
        "codex", start, metadata(), now=NOW, correlation_dir=tmp_path
    )
    finished = normalize_payload(
        "codex", finish, metadata(), now=NOW, correlation_dir=tmp_path
    )

    assert first == finished == []
    assert not (tmp_path / ".pending.json").exists()


def test_pending_correlation_expires_and_recovers(monkeypatch, tmp_path):
    monkeypatch.setattr(correlation.time, "time", lambda: 10.0)
    first, created = correlation.remember_pending("tool:key", directory=tmp_path)
    assert created
    monkeypatch.setattr(
        correlation.time,
        "time",
        lambda: 10.0 + correlation.PENDING_TTL_SECONDS + 1,
    )

    second, created = correlation.remember_pending("tool:key", directory=tmp_path)

    assert created
    assert second != first


def test_tool_finish_without_start_is_ignored(tmp_path):
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "s",
        "turn_id": "t",
        "tool_name": "exec_command",
    }
    records = normalize_payload(
        "codex", payload, metadata(), now=NOW, correlation_dir=tmp_path
    )
    assert records == []


def test_session_start_confirms_explicit_handoff_once(monkeypatch, tmp_path):
    monkeypatch.setenv("MERLIN_TIMELINE_TRACE_ID", "chain-trace")
    monkeypatch.setenv("MERLIN_TIMELINE_HANDOFF_ID", "handoff-span")
    records = normalize_payload(
        "codex",
        cases("codex")["session_start_startup"],
        metadata(),
        now=NOW,
        correlation_dir=tmp_path,
    )
    assert records[0]["trace_id"] == "chain-trace"
    assert records[1]["kind"] == "chain.handoff"
    assert records[1]["phase"] == "point"
    assert records[1]["parent_span_id"] == "handoff-span"

    repeated = normalize_payload(
        "codex",
        cases("codex")["session_resume"],
        metadata(),
        now=NOW,
        correlation_dir=tmp_path,
    )
    assert [record["kind"] for record in repeated] == ["session.lifecycle"]
