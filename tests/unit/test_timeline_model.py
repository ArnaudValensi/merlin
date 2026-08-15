"""Deterministic Timeline span, point, track, and anomaly assembly."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

from timeline.model import assemble
from timeline.schema import ActivityEvent


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def event(
    *,
    at: datetime = NOW,
    phase: str = "point",
    kind: str = "human.prompt",
    span: str | None = None,
    parent: str | None = None,
    trace: str = "trace",
    status: str = "ok",
    actor_type: str = "human",
    actor_id: str = "human",
    agent_sid: str | None = None,
    tmux_session: str | None = None,
    tmux_window: str | None = None,
    name: str = "Activity",
) -> ActivityEvent:
    return ActivityEvent.model_validate(
        {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "timestamp": at,
            "phase": phase,
            "kind": kind,
            "trace_id": trace,
            "span_id": span,
            "parent_span_id": parent,
            "actor": {
                "type": actor_type,
                "id": actor_id,
                "label": actor_id.title(),
            },
            "context": {
                key: value
                for key, value in {
                    "agent_sid": agent_sid,
                    "tmux_session": tmux_session,
                    "tmux_window": tmux_window,
                }.items()
                if value is not None
            },
            "status": status,
            "name": name,
            "attributes": {},
        }
    )


def test_pairs_spans_points_and_parent_children_with_stable_tracks():
    values = [
        event(name="Prompt"),
        event(
            phase="start",
            kind="agent.turn",
            span="turn",
            status="running",
            actor_type="agent",
            actor_id="agent-a",
            agent_sid="agent-a",
            name="Turn",
        ),
        event(
            at=NOW + timedelta(seconds=1),
            phase="start",
            kind="tool.call",
            span="tool",
            parent="turn",
            status="running",
            actor_type="automation",
            actor_id="automation:agent-a",
            agent_sid="agent-a",
            name="Tool",
        ),
        event(
            at=NOW + timedelta(seconds=2),
            phase="finish",
            kind="tool.call",
            span="tool",
            parent="turn",
            status="ok",
            actor_type="automation",
            actor_id="automation:agent-a",
            agent_sid="agent-a",
        ),
        event(
            at=NOW + timedelta(seconds=3),
            phase="finish",
            kind="agent.turn",
            span="turn",
            status="ok",
            actor_type="agent",
            actor_id="agent-a",
            agent_sid="agent-a",
        ),
    ]
    result = assemble(values, now=NOW + timedelta(seconds=4), live_agent_ids=set())
    assert [item["phase"] for item in result.items] == ["point", "span", "span"]
    turn = next(item for item in result.items if item["span_id"] == "turn")
    tool = next(item for item in result.items if item["span_id"] == "tool")
    assert turn["duration_ms"] == 3000
    assert tool["parent_id"] == turn["id"]
    assert turn["children"] == [tool["id"]]
    assert [track["id"] for track in result.tracks["participants"]] == [
        "human",
        "agent-a",
        "automation",
    ]
    assert len(result.updates) == 2


def test_open_span_uses_only_explicit_liveness():
    start = event(
        phase="start",
        kind="agent.turn",
        span="turn",
        status="running",
        actor_type="agent",
        actor_id="agent-a",
        agent_sid="agent-a",
    )
    alive = assemble(
        [start], now=NOW + timedelta(seconds=5), live_agent_ids={"agent-a"}
    ).items[0]
    dead = assemble(
        [start], now=NOW + timedelta(seconds=5), live_agent_ids=set()
    ).items[0]
    unknown = assemble(
        [start], now=NOW + timedelta(seconds=5), live_agent_ids=None
    ).items[0]
    assert (alive["open"], alive["status"], alive["duration_ms"]) == (
        True,
        "running",
        5000,
    )
    assert (dead["open"], dead["status"], dead["end_timestamp"]) == (
        False,
        "interrupted",
        None,
    )
    assert unknown["status"] == "unknown"
    assert unknown["anomaly"] == "liveness-unknown"


def test_open_span_without_agent_identity_has_unknown_liveness():
    start = event(
        phase="start",
        kind="review.await",
        span="review-wait",
        status="blocked",
        actor_type="automation",
        actor_id="clover-review",
    )

    item = assemble(
        [start],
        now=NOW + timedelta(seconds=5),
        live_agent_ids=set(),
        live_tmux_windows=set(),
    ).items[0]

    assert item["open"] is False
    assert item["status"] == "unknown"
    assert item["anomaly"] == "liveness-unavailable"


def test_open_span_falls_back_to_current_tmux_window_without_agent_sid():
    start = event(
        phase="start",
        kind="agent.turn",
        span="turn",
        status="running",
        actor_type="agent",
        actor_id="timeline:fallback",
        tmux_session="work",
        tmux_window="@7",
    )

    alive = assemble(
        [start],
        now=NOW + timedelta(seconds=5),
        live_agent_ids=set(),
        live_tmux_windows={("work", "@7")},
    ).items[0]
    dead = assemble(
        [start],
        now=NOW + timedelta(seconds=5),
        live_agent_ids=set(),
        live_tmux_windows=set(),
    ).items[0]
    unknown = assemble(
        [start],
        now=NOW + timedelta(seconds=5),
        live_agent_ids=None,
        live_tmux_windows=None,
    ).items[0]

    assert (alive["open"], alive["status"], alive["duration_ms"]) == (
        True,
        "running",
        5000,
    )
    assert (dead["open"], dead["status"], dead["anomaly"]) == (
        False,
        "interrupted",
        "actor-not-live",
    )
    assert (unknown["status"], unknown["anomaly"]) == (
        "unknown",
        "liveness-unknown",
    )


def test_finish_only_duplicate_boundaries_and_clock_skew_remain_visible():
    values = [
        event(phase="finish", kind="tool.call", span="missing", status="error"),
        event(
            at=NOW + timedelta(seconds=5),
            phase="start",
            kind="tool.call",
            span="skew",
            status="running",
        ),
        event(
            at=NOW + timedelta(seconds=4),
            phase="finish",
            kind="tool.call",
            span="skew",
            status="ok",
        ),
        event(
            at=NOW + timedelta(seconds=6),
            phase="finish",
            kind="tool.call",
            span="skew",
            status="error",
        ),
    ]
    result = assemble(values, now=NOW + timedelta(seconds=10), live_agent_ids=set())
    assert result.anomalies == 3
    assert {item["anomaly"] for item in result.items} == {
        "finish-without-start",
        "clock-skew",
        "duplicate-finish",
    }
    skew = next(item for item in result.items if item["phase"] == "span")
    assert skew["duration_ms"] == 0
    assert skew["end_timestamp"] == skew["start_timestamp"]


def test_equal_timestamps_keep_input_order_and_unknown_kinds_are_neutral():
    first = event(
        kind="future.provider",
        name="First",
        actor_type="automation",
        actor_id="automation",
    )
    second = event(
        kind="future.provider",
        name="Second",
        actor_type="automation",
        actor_id="automation",
    )
    result = assemble([second, first], now=NOW, live_agent_ids=set())
    assert [item["label"] for item in result.items] == ["Second", "First"]
    assert all(item["activity_track"] == "activity-tools" for item in result.items)


def test_whole_second_sorts_before_fractional_timestamp():
    whole = event(at=NOW, name="Whole")
    fraction = event(at=NOW + timedelta(milliseconds=250), name="Fraction")

    result = assemble([fraction, whole], now=NOW + timedelta(seconds=1))

    assert [item["label"] for item in result.items] == ["Whole", "Fraction"]


def test_trace_and_span_colons_cannot_collide_in_display_ids():
    first = event(
        phase="start", kind="tool.call", trace="a:b", span="c", status="running"
    )
    second = event(
        phase="start", kind="tool.call", trace="a", span="b:c", status="running"
    )

    result = assemble([first, second], now=NOW, live_agent_ids=set())

    assert len({item["id"] for item in result.items}) == 2


def test_dense_assembly_is_linear_enough_for_api_bounds():
    values = []
    for index in range(5000):
        at = NOW + timedelta(milliseconds=index)
        values.append(
            event(
                at=at,
                phase="start",
                kind="tool.call",
                span=f"tool-{index}",
                status="running",
                actor_type="automation",
                actor_id="automation:a",
                agent_sid="agent-a",
            )
        )
        values.append(
            event(
                at=at + timedelta(milliseconds=1),
                phase="finish",
                kind="tool.call",
                span=f"tool-{index}",
                status="ok",
                actor_type="automation",
                actor_id="automation:a",
                agent_sid="agent-a",
            )
        )
    started = time.perf_counter()
    result = assemble(values, now=NOW + timedelta(minutes=1), live_agent_ids=set())
    elapsed = time.perf_counter() - started
    assert len(result.items) == 5000
    assert elapsed < 2.0
