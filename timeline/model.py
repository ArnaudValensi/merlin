"""Pure activity-event assembly for Timeline API responses."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .schema import ActivityEvent


@dataclass(frozen=True)
class Assembly:
    """One bounded set of display items, tracks, and incremental span updates."""

    items: list[dict[str, Any]]
    tracks: dict[str, list[dict[str, Any]]]
    updates: list[dict[str, Any]]
    anomalies: int


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _span_id(event: ActivityEvent) -> str:
    return span_item_id(event.trace_id, event.span_id or "")


def span_item_id(trace_id: str, span_id: str) -> str:
    """Return an unambiguous opaque display id for one trace/span pair."""
    value = f"{trace_id}\0{span_id}".encode()
    return f"span:{hashlib.sha256(value).hexdigest()[:32]}"


def _point_id(event: ActivityEvent) -> str:
    return f"event:{event.event_id}"


def _activity_track(event: ActivityEvent) -> str:
    if event.actor.type == "human":
        return "activity-human"
    if event.kind in {"agent.turn", "agent.session", "session.lifecycle"}:
        return "activity-agent"
    if event.kind in {"agent.wait", "review.await"}:
        return "activity-wait"
    if event.kind.startswith("review.") or event.kind.startswith("chain."):
        return "activity-review"
    return "activity-automation"


def _participant_track(event: ActivityEvent) -> str:
    return event.actor.id if event.actor.type == "agent" else event.actor.type


def _base_item(event: ActivityEvent) -> dict[str, Any]:
    context = event.context.model_dump(mode="json", exclude_none=True)
    actor = event.actor.model_dump(mode="json", exclude_none=True)
    attributes = event.attributes
    parent_id = (
        span_item_id(event.trace_id, event.parent_span_id)
        if event.parent_span_id
        else None
    )
    return {
        "kind": event.kind,
        "trace_id": event.trace_id,
        "span_id": event.span_id,
        "parent_id": parent_id,
        "children": [],
        "actor": actor["type"],
        "actor_id": actor["id"],
        "actor_label": actor["label"],
        "role": actor.get("role"),
        "participant_track": _participant_track(event),
        "activity_track": _activity_track(event),
        "label": event.name,
        "context": context,
        "attributes": attributes,
        "source": attributes.get("provider_event", "emitter"),
    }


def _point(event: ActivityEvent, order: int, anomaly: str | None = None) -> dict:
    item = {
        **_base_item(event),
        "id": _point_id(event),
        "phase": "point",
        "start_timestamp": _iso(event.timestamp),
        "end_timestamp": None,
        "duration_ms": 0,
        "status": event.status,
        "open": False,
        "anomaly": anomaly,
        "_order": order,
        "_sort_timestamp": event.timestamp,
    }
    return item


def _finish_update(event: ActivityEvent, item: dict) -> dict[str, Any]:
    return {
        "id": _span_id(event),
        "end_timestamp": item.get("end_timestamp") or _iso(event.timestamp),
        "duration_ms": item.get("duration_ms"),
        "status": item.get("status", event.status),
        "anomaly": item.get("anomaly"),
        "kind": event.kind,
        "trace_id": event.trace_id,
        "actor": event.actor.type,
        "actor_id": event.actor.id,
        "project": event.context.project,
        "provider": event.context.provider,
    }


def _span(
    start: ActivityEvent,
    finish: ActivityEvent | None,
    order: int,
    *,
    now: datetime,
    live_agent_ids: set[str] | None,
    inactive_agent_ids: set[str] | None,
    live_tmux_windows: set[tuple[str, str]] | None,
    superseded_at: datetime | None,
) -> tuple[dict, str | None]:
    item = {
        **_base_item(start),
        "id": _span_id(start),
        "phase": "span",
        "start_timestamp": _iso(start.timestamp),
        "end_timestamp": _iso(finish.timestamp) if finish else None,
        "duration_ms": None,
        "status": finish.status if finish else start.status,
        "open": False,
        "anomaly": None,
        "_order": order,
        "_sort_timestamp": start.timestamp,
    }
    anomaly = None
    if finish is not None:
        duration = (finish.timestamp - start.timestamp).total_seconds() * 1000
        if duration < 0:
            duration = 0
            item["end_timestamp"] = item["start_timestamp"]
            item["anomaly"] = anomaly = "clock-skew"
        item["duration_ms"] = round(duration)
        return item, anomaly

    if superseded_at is not None:
        duration = max(0, (superseded_at - start.timestamp).total_seconds() * 1000)
        item["end_timestamp"] = _iso(superseded_at)
        item["duration_ms"] = round(duration)
        item["status"] = "interrupted"
        item["anomaly"] = anomaly = "superseded-turn"
        return item, anomaly

    agent_sid = start.context.agent_sid
    if agent_sid is None:
        tmux_session = start.context.tmux_session
        tmux_window = start.context.tmux_window
        if (
            tmux_session is not None
            and tmux_window is not None
            and live_tmux_windows is not None
        ):
            tmux_identity = (tmux_session, tmux_window)
            if tmux_identity in live_tmux_windows:
                item["open"] = True
                item["duration_ms"] = max(
                    0, round((now - start.timestamp).total_seconds() * 1000)
                )
            else:
                item["status"] = "interrupted"
                item["anomaly"] = anomaly = "actor-not-live"
        elif live_tmux_windows is None:
            item["status"] = "unknown"
            item["anomaly"] = anomaly = "liveness-unknown"
        else:
            item["status"] = "unknown"
            item["anomaly"] = anomaly = "liveness-unavailable"
    elif inactive_agent_ids is not None and agent_sid in inactive_agent_ids:
        item["status"] = "interrupted"
        item["anomaly"] = anomaly = "actor-inactive"
    elif live_agent_ids is not None and agent_sid in live_agent_ids:
        item["open"] = True
        item["duration_ms"] = max(
            0, round((now - start.timestamp).total_seconds() * 1000)
        )
    elif live_agent_ids is None:
        item["status"] = "unknown"
        item["anomaly"] = anomaly = "liveness-unknown"
    else:
        item["status"] = "interrupted"
        item["anomaly"] = anomaly = "actor-not-live"
    return item, anomaly


def tracks_for_items(items: list[dict]) -> dict[str, list[dict]]:
    participants = []
    if any(item["actor"] == "human" for item in items):
        participants.append({"id": "human", "name": "Human", "meta": "interventions"})
    seen_agents: set[str] = set()
    for item in items:
        if item["actor"] != "agent" or item["actor_id"] in seen_agents:
            continue
        seen_agents.add(item["actor_id"])
        participants.append(
            {
                "id": item["actor_id"],
                "name": item["actor_label"],
                "meta": item["context"].get("agent_sid", "agent"),
                "role": item.get("role"),
            }
        )
    if any(item["actor"] == "automation" for item in items):
        participants.append(
            {"id": "automation", "name": "Automation", "meta": "workflow"}
        )
    activity = [
        {"id": "activity-human", "name": "Human input", "meta": "points"},
        {"id": "activity-agent", "name": "Agent work", "meta": "turns"},
        {"id": "activity-wait", "name": "Waiting", "meta": "blocked"},
        {
            "id": "activity-review",
            "name": "Review & handoff",
            "meta": "coordination",
        },
        {
            "id": "activity-automation",
            "name": "Automation",
            "meta": "explicit events",
        },
    ]
    present_activity = {item["activity_track"] for item in items}
    return {
        "participants": participants,
        "activity": [track for track in activity if track["id"] in present_activity],
    }


def assemble(
    events: list[ActivityEvent],
    *,
    now: datetime | None = None,
    live_agent_ids: set[str] | None = None,
    inactive_agent_ids: set[str] | None = None,
    live_tmux_windows: set[tuple[str, str]] | None = None,
) -> Assembly:
    """Pair boundaries once and retain every malformed lifecycle visibly."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ordered = sorted(enumerate(events), key=lambda pair: pair[1].timestamp)
    points: list[tuple[int, ActivityEvent]] = []
    span_groups: dict[tuple[str, str], list[tuple[int, ActivityEvent]]] = {}
    for order, event in ordered:
        if event.phase == "point":
            points.append((order, event))
        else:
            span_groups.setdefault((event.trace_id, event.span_id or ""), []).append(
                (order, event)
            )

    first_turn_starts: dict[tuple[str, str], ActivityEvent] = {}
    for boundaries in span_groups.values():
        for _order, event in boundaries:
            if event.phase == "start" and event.kind == "agent.turn":
                first_turn_starts.setdefault(
                    (event.trace_id, event.span_id or ""), event
                )
                break
    turns_by_actor: dict[tuple[str, str], list[ActivityEvent]] = {}
    for event in first_turn_starts.values():
        turns_by_actor.setdefault((event.trace_id, event.actor.id), []).append(event)
    superseded_turns: dict[tuple[str, str], datetime] = {}
    for turns in turns_by_actor.values():
        turns.sort(key=lambda event: (event.timestamp, event.event_id))
        for current, following in zip(turns, turns[1:], strict=False):
            superseded_turns[(current.trace_id, current.span_id or "")] = (
                following.timestamp
            )

    items = [_point(event, order) for order, event in points]
    updates: list[dict[str, Any]] = []
    anomalies = 0
    for boundaries in span_groups.values():
        starts = [
            (order, event) for order, event in boundaries if event.phase == "start"
        ]
        finishes = [
            (order, event) for order, event in boundaries if event.phase == "finish"
        ]
        if starts:
            start_order, start = starts[0]
            finish = finishes[0][1] if finishes else None
            item, anomaly = _span(
                start,
                finish,
                start_order,
                now=now,
                live_agent_ids=live_agent_ids,
                inactive_agent_ids=inactive_agent_ids,
                live_tmux_windows=live_tmux_windows,
                superseded_at=superseded_turns.get(
                    (start.trace_id, start.span_id or "")
                ),
            )
            items.append(item)
            if anomaly:
                anomalies += 1
            if finish:
                updates.append(_finish_update(finish, item))
            for order, duplicate in starts[1:]:
                items.append(_point(duplicate, order, "duplicate-start"))
                anomalies += 1
            for order, duplicate in finishes[1:]:
                items.append(_point(duplicate, order, "duplicate-finish"))
                anomalies += 1
        else:
            first_order, first = finishes[0]
            items.append(_point(first, first_order, "finish-without-start"))
            updates.append(_finish_update(first, {}))
            anomalies += 1
            for order, duplicate in finishes[1:]:
                items.append(_point(duplicate, order, "duplicate-finish"))
                anomalies += 1

    items.sort(key=lambda item: (item["_sort_timestamp"], item["_order"]))
    by_id = {item["id"]: item for item in items}
    for item in items:
        parent = by_id.get(item.get("parent_id"))
        if parent is not None:
            parent["children"].append(item["id"])
    for item in items:
        item.pop("_order")
        item.pop("_sort_timestamp")
    return Assembly(
        items=items,
        tracks=tracks_for_items(items),
        updates=updates,
        anomalies=anomalies,
    )
