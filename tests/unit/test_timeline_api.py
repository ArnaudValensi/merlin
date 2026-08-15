"""Authenticated, bounded, filterable, incremental Timeline API."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import auth
import main as app_mod
from timeline import routes
from timeline.store import ActivityStore


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("MERLIN_TIMELINE_FIXTURES", raising=False)
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    monkeypatch.setenv("MERLIN_ACTIVITY_HOOKS", "auto")
    monkeypatch.setattr(routes, "_live_actors", lambda: ({"agent-a"}, set()))
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    auth.configure("")
    with TestClient(app_mod.app) as test_client:
        yield test_client
    auth.configure("")


def event(
    *,
    at: datetime = NOW,
    kind: str = "human.prompt",
    phase: str = "point",
    span: str | None = None,
    status: str = "ok",
    actor_type: str = "human",
    actor_id: str = "human",
    trace: str = "trace-a",
    project: str = "alpha",
    provider: str = "Codex",
    name: str = "Activity",
    tmux_session: str | None = None,
    tmux_window: str | None = None,
    include_agent_sid: bool = True,
) -> dict:
    return {
        "schema_version": 1,
        "event_id": str(uuid.uuid4()),
        "timestamp": at.isoformat(),
        "phase": phase,
        "kind": kind,
        "trace_id": trace,
        "span_id": span,
        "actor": {
            "type": actor_type,
            "id": actor_id,
            "label": actor_id.title(),
        },
        "context": {
            "project": project,
            "provider": provider,
            **({"tmux_session": tmux_session} if tmux_session else {}),
            **({"tmux_window": tmux_window} if tmux_window else {}),
            **(
                {"agent_sid": actor_id}
                if actor_type == "agent" and include_agent_sid
                else {}
            ),
        },
        "status": status,
        "name": name,
        "attributes": {},
    }


def query_range(**extra) -> dict:
    return {
        "since": (NOW - timedelta(hours=1)).isoformat(),
        "until": (NOW + timedelta(hours=1)).isoformat(),
        **extra,
    }


def test_liveness_failure_is_unknown_not_dead(monkeypatch):
    from board import sweep

    def fail_sweep():
        raise OSError("tmux unavailable")

    monkeypatch.setattr(sweep, "run_sweep_checked", fail_sweep)

    assert routes._live_actors() is None


def seed() -> None:
    store = ActivityStore()
    values = [
        event(name="Prompt"),
        event(
            kind="agent.turn",
            phase="start",
            span="turn",
            status="running",
            actor_type="agent",
            actor_id="agent-a",
            name="Turn",
        ),
        event(
            at=NOW + timedelta(seconds=3),
            kind="agent.turn",
            phase="finish",
            span="turn",
            actor_type="agent",
            actor_id="agent-a",
            name="Turn complete",
        ),
        event(
            at=NOW + timedelta(seconds=4),
            kind="tool.call",
            phase="start",
            span="tool",
            status="running",
            actor_type="automation",
            actor_id="automation",
            trace="trace-b",
            project="beta",
            provider="Claude Code",
            name="Tool",
        ),
        event(
            at=NOW + timedelta(seconds=5),
            kind="tool.call",
            phase="finish",
            span="tool",
            status="error",
            actor_type="automation",
            actor_id="automation",
            trace="trace-b",
            project="beta",
            provider="Claude Code",
            name="Tool failed",
        ),
    ]
    for value in values:
        assert store.append(value, strict=True).ok


def test_live_api_returns_tracks_pairs_bounds_and_cursor(client):
    seed()
    value = client.get("/api/timeline", params=query_range()).json()
    assert value["state"] == "ready"
    assert [item["phase"] for item in value["items"]] == [
        "point",
        "span",
        "span",
    ]
    assert value["items"][1]["duration_ms"] == 3000
    assert value["cursor"]
    assert value["lanes"] == value["tracks"]["participants"]
    assert value["range"]["seconds"] == 7200
    assert value["partial"] is False
    assert value["skipped"] == value["flagged"] == value["dropped"] == 0


def test_unreadable_and_flagged_counts_are_distinct(client, monkeypatch):
    store = ActivityStore()
    store.append(
        event(
            kind="agent.turn",
            phase="start",
            span="open",
            status="running",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )
    path = next(store.directory.glob("*.jsonl"))
    with path.open("a") as handle:
        handle.write("{broken\n")
    monkeypatch.setattr(routes, "_live_actors", lambda: None)

    value = client.get("/api/timeline", params=query_range()).json()

    assert value["skipped"] == 1
    assert value["flagged"] == 1
    assert value["anomalies"] == 2
    assert value["items"][0]["anomaly"] == "liveness-unknown"


def test_capture_health_is_returned_separately(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        "read_capture_health",
        lambda _directory: {
            "dropped": 3,
            "last_error": "daily-cap",
            "updated_at": "2026-08-15T12:00:00Z",
        },
    )

    value = client.get("/api/timeline", params=query_range()).json()

    assert value["dropped"] == 3
    assert value["capture_health"]["last_error"] == "daily-cap"


@pytest.mark.parametrize(
    ("mode", "filters", "expected"),
    [
        ("off", {}, "collector-disabled"),
        ("auto", {}, "empty"),
        ("auto", {"kind": "missing.kind"}, "no-results"),
    ],
)
def test_incremental_empty_responses_preserve_truthful_state(
    client, monkeypatch, mode, filters, expected
):
    monkeypatch.setattr(routes, "capture_mode", lambda: mode)
    first = client.get("/api/timeline", params=query_range(**filters)).json()
    second = client.get(
        "/api/timeline",
        params=query_range(cursor=first["cursor"], **filters),
    ).json()

    assert first["state"] == second["state"] == expected
    assert second["items"] == []


@pytest.mark.parametrize(
    ("parameter", "value", "expected"),
    [
        ("actor", "agent-a", {"agent.turn"}),
        ("actor", "automation", {"tool.call"}),
        ("kind", "human.prompt", {"human.prompt"}),
        ("status", "error", {"tool.call"}),
        ("project", "beta", {"tool.call"}),
        ("provider", "claude code", {"tool.call"}),
        ("trace", "trace-b", {"tool.call"}),
    ],
)
def test_each_filter_is_applied_after_assembly(client, parameter, value, expected):
    seed()
    response = client.get("/api/timeline", params=query_range(**{parameter: value}))
    assert response.status_code == 200
    assert {item["kind"] for item in response.json()["items"]} == expected


def test_combined_filters_grouping_and_no_results(client):
    seed()
    response = client.get(
        "/api/timeline",
        params=query_range(
            grouping="activity",
            actor="automation",
            status="error",
            project="beta",
        ),
    )
    value = response.json()
    assert len(value["items"]) == 1
    assert value["lanes"] == value["tracks"]["activity"]
    empty = client.get("/api/timeline", params=query_range(kind="missing.kind")).json()
    assert empty["state"] == "no-results"


@pytest.mark.parametrize(
    "params",
    [
        {"since": NOW.isoformat(), "until": (NOW - timedelta(seconds=1)).isoformat()},
        {
            "since": NOW.isoformat(),
            "until": (NOW + timedelta(days=8)).isoformat(),
        },
        {"since": "2026-08-15T12:00:00", "until": NOW.isoformat()},
        {"limit": 10001},
        {"limit": 0},
        {"grouping": "unknown"},
        {"cursor": "broken"},
    ],
)
def test_invalid_queries_return_useful_422(client, params):
    response = client.get("/api/timeline", params=params)
    assert response.status_code == 422
    assert response.json()["detail"]


def test_cursor_reads_late_append_across_midnight_without_rereading(client):
    store = ActivityStore()
    before = NOW.replace(hour=23, minute=59)
    after = before + timedelta(minutes=2)
    store.append(event(at=before, name="Before"), strict=True)
    store.append(event(at=after, name="After"), strict=True)
    params = {
        "since": (before - timedelta(minutes=1)).isoformat(),
        "until": (after + timedelta(minutes=2)).isoformat(),
    }
    first = client.get("/api/timeline", params=params).json()
    assert [item["label"] for item in first["items"]] == ["Before", "After"]
    late = after + timedelta(seconds=1)
    store.append(event(at=late, name="Late"), strict=True)
    second = client.get(
        "/api/timeline", params={**params, "cursor": first["cursor"]}
    ).json()
    assert [item["label"] for item in second["items"]] == ["Late"]


def test_malformed_lines_unknown_kinds_and_auth_are_preserved(client, monkeypatch):
    store = ActivityStore()
    store.append(event(kind="future.provider", name="Future"), strict=True)
    path = next(store.directory.glob("*.jsonl"))
    with path.open("a") as handle:
        handle.write("{broken\n")
    value = client.get("/api/timeline", params=query_range()).json()
    assert value["items"][0]["kind"] == "future.provider"
    assert value["anomalies"] == 1
    assert value["skipped"] == 1
    assert value["flagged"] == 0

    auth.configure("secret")
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
    assert (
        client.get(
            "/api/timeline", params=query_range(), follow_redirects=False
        ).status_code
        == 303
    )


def test_incremental_finish_returns_boundary_update_for_existing_span(client):
    store = ActivityStore()
    store.append(
        event(
            kind="agent.turn",
            phase="start",
            span="turn",
            status="running",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )
    first = client.get("/api/timeline", params=query_range()).json()
    item_id = first["items"][0]["id"]
    store.append(
        event(
            at=NOW + timedelta(seconds=2),
            kind="agent.turn",
            phase="finish",
            span="turn",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )
    second = client.get(
        "/api/timeline",
        params=query_range(cursor=first["cursor"]),
    ).json()
    assert second["updates"][0]["id"] == item_id
    assert second["updates"][0]["status"] == "ok"
    assert second["items"] == []
    assert second["anomalies"] == 0


def test_full_query_includes_live_span_started_before_range(client, monkeypatch):
    store = ActivityStore()
    store.append(
        event(
            at=NOW - timedelta(hours=2),
            kind="agent.turn",
            phase="start",
            span="long-turn",
            status="running",
            actor_type="agent",
            actor_id="agent-a",
            name="Long turn",
        ),
        strict=True,
    )
    params = {
        "since": (NOW - timedelta(hours=1)).isoformat(),
        "until": (NOW + timedelta(minutes=1)).isoformat(),
    }

    live = client.get("/api/timeline", params=params).json()

    assert live["state"] == "ready"
    assert len(live["items"]) == 1
    assert live["items"][0]["open"] is True
    assert live["items"][0]["continues_before_range"] is True
    assert live["items"][0]["start"] == -3600

    monkeypatch.setattr(routes, "_live_actors", lambda: (set(), set()))
    dead = client.get("/api/timeline", params=params).json()

    assert dead["state"] == "empty"
    assert dead["items"] == []
    assert dead["updates"][0]["id"] == live["items"][0]["id"]
    assert dead["updates"][0]["status"] == "interrupted"
    assert dead["updates"][0]["open"] is False


def test_full_query_pairs_span_that_finishes_inside_range(client):
    store = ActivityStore()
    store.append(
        event(
            at=NOW - timedelta(hours=2),
            kind="agent.turn",
            phase="start",
            span="crossing-turn",
            status="running",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )
    store.append(
        event(
            at=NOW - timedelta(minutes=30),
            kind="agent.turn",
            phase="finish",
            span="crossing-turn",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )

    value = client.get(
        "/api/timeline",
        params={
            "since": (NOW - timedelta(hours=1)).isoformat(),
            "until": NOW.isoformat(),
        },
    ).json()

    assert len(value["items"]) == 1
    assert value["items"][0]["continues_before_range"] is True
    assert value["items"][0]["duration_ms"] == 90 * 60 * 1000


def test_historical_query_uses_known_finish_after_range(client):
    store = ActivityStore()
    store.append(
        event(
            at=NOW - timedelta(hours=3),
            kind="agent.turn",
            phase="start",
            span="historical-turn",
            status="running",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )
    store.append(
        event(
            at=NOW - timedelta(minutes=30),
            kind="agent.turn",
            phase="finish",
            span="historical-turn",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )

    value = client.get(
        "/api/timeline",
        params={
            "since": (NOW - timedelta(hours=2)).isoformat(),
            "until": (NOW - timedelta(hours=1)).isoformat(),
        },
    ).json()

    assert value["state"] == "ready"
    assert len(value["items"]) == 1
    assert value["items"][0]["status"] == "ok"
    assert value["items"][0]["open"] is False
    assert value["items"][0]["end"] == 90 * 60
    assert value["items"][0]["duration_ms"] == 150 * 60 * 1000


def test_historical_query_pairs_future_finish_for_start_inside_range(
    client, monkeypatch
):
    store = ActivityStore()
    store.append(
        event(
            at=NOW - timedelta(minutes=50),
            kind="agent.turn",
            phase="start",
            span="inside-turn",
            status="running",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )
    store.append(
        event(
            at=NOW + timedelta(minutes=10),
            kind="agent.turn",
            phase="finish",
            span="inside-turn",
            status="ok",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )
    monkeypatch.setattr(routes, "_live_actors", lambda: (set(), set()))

    value = client.get(
        "/api/timeline",
        params={
            "since": (NOW - timedelta(hours=1)).isoformat(),
            "until": (NOW - timedelta(minutes=20)).isoformat(),
        },
    ).json()

    assert len(value["items"]) == 1
    assert value["items"][0]["status"] == "ok"
    assert value["items"][0]["open"] is False
    assert value["items"][0]["duration_ms"] == 3_600_000
    assert value["flagged"] == 0


def test_truncated_historical_query_does_not_flag_hidden_future_finish(client):
    store = ActivityStore()
    since = NOW - timedelta(hours=2)
    until = NOW - timedelta(hours=1)
    store.append(event(at=since + timedelta(minutes=1), name="Visible"), strict=True)
    store.append(
        event(
            at=since + timedelta(minutes=10),
            kind="agent.turn",
            phase="start",
            span="truncated-turn",
            status="running",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )
    store.append(
        event(
            at=until + timedelta(minutes=10),
            kind="agent.turn",
            phase="finish",
            span="truncated-turn",
            status="ok",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )

    value = client.get(
        "/api/timeline",
        params={
            "since": since.isoformat(),
            "until": until.isoformat(),
            "limit": 1,
        },
    ).json()

    assert value["partial"] is True
    assert [item["label"] for item in value["items"]] == ["Visible"]
    assert value["flagged"] == 0
    assert value["anomalies"] == 0


def test_tmux_window_liveness_keeps_capture_independent_of_state_pills(
    client, monkeypatch
):
    store = ActivityStore()
    store.append(
        event(
            kind="agent.turn",
            phase="start",
            span="fallback-turn",
            status="running",
            actor_type="agent",
            actor_id="timeline:fallback",
            include_agent_sid=False,
            tmux_session="work",
            tmux_window="@7",
        ),
        strict=True,
    )
    monkeypatch.setattr(routes, "_live_actors", lambda: (set(), {("work", "@7")}))

    value = client.get("/api/timeline", params=query_range()).json()

    assert value["items"][0]["open"] is True
    assert value["items"][0]["status"] == "running"
    assert value["items"][0]["context"].get("agent_sid") is None


@pytest.mark.parametrize(
    ("live_actors", "expected"),
    [
        (None, "liveness-unknown"),
        ((set(), set()), "actor-not-live"),
    ],
)
def test_crossing_span_returns_status_update_when_not_live(
    client, monkeypatch, live_actors, expected
):
    store = ActivityStore()
    store.append(
        event(
            at=NOW - timedelta(hours=2),
            kind="agent.turn",
            phase="start",
            span="old-turn",
            status="running",
            actor_type="agent",
            actor_id="agent-a",
        ),
        strict=True,
    )
    monkeypatch.setattr(routes, "_live_actors", lambda: live_actors)

    value = client.get(
        "/api/timeline",
        params={
            "since": (NOW - timedelta(hours=1)).isoformat(),
            "until": NOW.isoformat(),
        },
    ).json()

    assert value["items"] == []
    assert value["updates"][0]["anomaly"] == expected
    assert value["updates"][0]["open"] is False


def test_span_context_rebuild_anomaly_is_reported(client):
    store = ActivityStore()
    store.append(event(name="Visible"), strict=True)
    client.get("/api/timeline", params=query_range())
    (store.directory / ".span-context.json").write_text("{broken\n")

    value = client.get("/api/timeline", params=query_range()).json()

    assert value["skipped"] == 1
    assert value["anomalies"] == 1


def test_incremental_orphan_finish_remains_visible_and_flagged(client):
    store = ActivityStore()
    first = client.get("/api/timeline", params=query_range()).json()
    store.append(
        event(
            at=NOW + timedelta(seconds=1),
            kind="tool.call",
            phase="finish",
            span="orphan",
            status="error",
            actor_type="automation",
            actor_id="automation",
        ),
        strict=True,
    )

    value = client.get(
        "/api/timeline", params=query_range(cursor=first["cursor"])
    ).json()

    assert len(value["items"]) == 1
    assert value["items"][0]["anomaly"] == "finish-without-start"
    assert value["flagged"] == 1
    assert value["anomalies"] == 1
