"""Timeline page, consent API, and bounded live activity query."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from merlin_ext import make_templates

from .consent import capture_mode, capture_setting, set_capture_mode
from .model import assemble, span_item_id, tracks_for_items
from .reconcile import hooks_drift, sync_hooks
from .store import ActivityStore, ActivityStoreError
from .writer import read_capture_health


TIMELINE_DIR = Path(__file__).parent.resolve()
STATIC_DIR = TIMELINE_DIR / "static"
TEMPLATES_DIR = TIMELINE_DIR / "templates"
templates = make_templates(TEMPLATES_DIR)

DEFAULT_RANGE = timedelta(hours=1)
MAX_RANGE = timedelta(days=7)
DEFAULT_LIMIT = 2000
MAX_LIMIT = 10000

api_router = APIRouter()
page_router = APIRouter()


class ConsentUpdate(BaseModel):
    mode: str


def _fixture_enabled() -> bool:
    """Require an explicit process setting; a URL cannot enable fixture data."""
    return os.getenv("MERLIN_TIMELINE_FIXTURES") == "1"


def _store() -> ActivityStore:
    return ActivityStore()


def _live_actors() -> tuple[set[str], set[tuple[str, str]]] | None:
    try:
        from board.sweep import run_sweep_checked

        windows = run_sweep_checked()
        if windows is None:
            return None
        return (
            {window.sid for window in windows if window.sid},
            {(window.session, window.window_id) for window in windows},
        )
    except Exception:
        return None


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status_code=422, detail=f"{name} must include a UTC offset")
    return value.astimezone(timezone.utc)


def _iso_query(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _matches(
    item: dict[str, Any],
    *,
    actor: str | None,
    kind: str | None,
    status: str | None,
    project: str | None,
    provider: str | None,
    trace: str | None,
) -> bool:
    context = item.get("context", {})
    return all(
        (
            actor is None or actor in {item.get("actor"), item.get("actor_id")},
            kind is None or item.get("kind") == kind,
            status is None or item.get("status") == status,
            project is None or context.get("project") == project,
            provider is None
            or str(context.get("provider", "")).casefold() == provider.casefold(),
            trace is None or item.get("trace_id") == trace,
        )
    )


def _update_matches(
    update: dict[str, Any],
    *,
    actor: str | None,
    kind: str | None,
    status: str | None,
    project: str | None,
    provider: str | None,
    trace: str | None,
) -> bool:
    return all(
        (
            actor is None or actor in {update.get("actor"), update.get("actor_id")},
            kind is None or update.get("kind") == kind,
            status is None or update.get("status") == status,
            project is None or update.get("project") == project,
            provider is None
            or str(update.get("provider", "")).casefold() == provider.casefold(),
            trace is None or update.get("trace_id") == trace,
        )
    )


def _relative(item: dict[str, Any], since: datetime) -> dict[str, Any]:
    output = dict(item)
    start = datetime.fromisoformat(item["start_timestamp"].replace("Z", "+00:00"))
    output["start"] = (start - since).total_seconds()
    output["continues_before_range"] = start < since
    if item.get("end_timestamp"):
        end = datetime.fromisoformat(item["end_timestamp"].replace("Z", "+00:00"))
        output["end"] = (end - since).total_seconds()
    else:
        output["end"] = None
    return output


def _intersects_range(item: dict[str, Any], since: datetime, until: datetime) -> bool:
    start = datetime.fromisoformat(item["start_timestamp"].replace("Z", "+00:00"))
    if item["phase"] == "point":
        return since <= start <= until
    if start > until:
        return False
    if item.get("end_timestamp"):
        end = datetime.fromisoformat(item["end_timestamp"].replace("Z", "+00:00"))
        return end >= since
    return start >= since or item.get("open") is True


def _stale_span_update(item: dict[str, Any]) -> dict[str, Any]:
    context = item.get("context", {})
    return {
        "id": item["id"],
        "end_timestamp": None,
        "duration_ms": None,
        "status": item["status"],
        "anomaly": item.get("anomaly"),
        "kind": item["kind"],
        "trace_id": item["trace_id"],
        "actor": item["actor"],
        "actor_id": item["actor_id"],
        "project": context.get("project"),
        "provider": context.get("provider"),
        "open": False,
    }


def _fixture_data() -> dict:
    return {
        "state": "ready",
        "source": "deterministic-fixture",
        "range": {
            "start": "2026-08-15T11:40:00Z",
            "end": "2026-08-15T11:48:00Z",
            "now": "2026-08-15T11:47:32Z",
        },
        "items": [
            {
                "id": "prompt-1",
                "phase": "point",
                "kind": "human.prompt",
                "start": 24,
                "actor": "human",
                "actor_id": "human",
                "label": "Prompt submitted",
                "status": "ok",
                "trace_id": "trace-clover-listen",
            },
            {
                "id": "turn-l2",
                "phase": "span",
                "kind": "agent.turn",
                "start": 24,
                "end": 226,
                "actor": "agent",
                "actor_id": "agent-codex-l2",
                "actor_label": "Codex · Listen-L2",
                "label": "Implement checkpoint L2",
                "status": "ok",
                "trace_id": "trace-clover-listen",
                "context": {
                    "role": "Implementer",
                    "provider": "Codex",
                    "model": "gpt-5.6-sol",
                    "effort": "xhigh",
                    "project": "clover",
                    "cwd": "/home/arnaud/dev/clover",
                    "tmux": "clover / listen-l2 / %31",
                    "agent_sid": "@l2-4fd2",
                },
            },
            {
                "id": "tool-inspect",
                "phase": "span",
                "kind": "tool.call",
                "start": 42,
                "end": 62,
                "actor": "automation",
                "actor_id": "automation",
                "label": "Inspect source",
                "status": "ok",
                "parent_id": "turn-l2",
                "trace_id": "trace-clover-listen",
            },
            {
                "id": "tool-build",
                "phase": "span",
                "kind": "automation.script",
                "start": 76,
                "end": 151,
                "actor": "automation",
                "actor_id": "automation",
                "label": "Affected checks",
                "status": "ok",
                "parent_id": "turn-l2",
                "trace_id": "trace-clover-listen",
            },
            {
                "id": "tool-failed",
                "phase": "span",
                "kind": "tool.call",
                "start": 161,
                "end": 178,
                "actor": "automation",
                "actor_id": "automation",
                "label": "Build failed",
                "status": "error",
                "parent_id": "turn-l2",
                "trace_id": "trace-clover-listen",
            },
            {
                "id": "review-request",
                "phase": "point",
                "kind": "review.request",
                "start": 228,
                "actor": "automation",
                "actor_id": "automation",
                "label": "Review L2-004 requested",
                "status": "ok",
                "trace_id": "trace-clover-listen",
            },
            {
                "id": "review-wait",
                "phase": "span",
                "kind": "review.await",
                "start": 236,
                "end": 326,
                "actor": "automation",
                "actor_id": "automation",
                "label": "Awaiting reviewer",
                "status": "blocked",
                "trace_id": "trace-clover-listen",
            },
            {
                "id": "review-turn",
                "phase": "span",
                "kind": "agent.turn",
                "start": 250,
                "end": 318,
                "actor": "agent",
                "actor_id": "agent-codex-review",
                "actor_label": "Codex · Review",
                "label": "Review frozen commit",
                "status": "ok",
                "trace_id": "trace-clover-listen",
                "context": {
                    "role": "Reviewer",
                    "provider": "Codex",
                    "model": "gpt-5.6-sol",
                    "effort": "xhigh",
                    "project": "clover",
                    "cwd": "/home/arnaud/dev/clover",
                    "tmux": "clover / listen-review / %32",
                    "agent_sid": "@rv-1aa8",
                },
            },
            {
                "id": "review-read",
                "phase": "span",
                "kind": "tool.call",
                "start": 267,
                "end": 286,
                "actor": "automation",
                "actor_id": "automation",
                "label": "Read diff",
                "status": "ok",
                "parent_id": "review-turn",
                "trace_id": "trace-clover-listen",
            },
            {
                "id": "review-complete",
                "phase": "point",
                "kind": "review.complete",
                "start": 326,
                "actor": "automation",
                "actor_id": "automation",
                "label": "Review accepted",
                "status": "ok",
                "trace_id": "trace-clover-listen",
            },
            {
                "id": "answer-1",
                "phase": "point",
                "kind": "human.answer",
                "start": 346,
                "actor": "human",
                "actor_id": "human",
                "label": "Approved handoff",
                "status": "ok",
                "trace_id": "trace-clover-listen",
            },
            {
                "id": "handoff",
                "phase": "span",
                "kind": "chain.handoff",
                "start": 354,
                "end": 371,
                "actor": "automation",
                "actor_id": "automation",
                "label": "Launch Listen-L3",
                "status": "ok",
                "trace_id": "trace-clover-listen",
            },
            {
                "id": "turn-l3",
                "phase": "span",
                "kind": "agent.turn",
                "start": 371,
                "end": None,
                "actor": "agent",
                "actor_id": "agent-codex-l3",
                "actor_label": "Codex · Listen-L3",
                "label": "Implement checkpoint L3",
                "status": "running",
                "trace_id": "trace-clover-listen",
                "context": {
                    "role": "Implementer",
                    "provider": "Codex",
                    "model": "gpt-5.6-sol",
                    "effort": "xhigh",
                    "project": "clover",
                    "cwd": "/home/arnaud/dev/clover",
                    "tmux": "clover / listen-l3 / %33",
                    "agent_sid": "@l3-0e91",
                },
            },
            {
                "id": "tool-open",
                "phase": "span",
                "kind": "tool.call",
                "start": 404,
                "end": None,
                "actor": "automation",
                "actor_id": "automation",
                "label": "Run targeted checks",
                "status": "running",
                "parent_id": "turn-l3",
                "trace_id": "trace-clover-listen",
            },
            {
                "id": "turn-claude",
                "phase": "span",
                "kind": "agent.turn",
                "start": 105,
                "end": 199,
                "actor": "agent",
                "actor_id": "agent-claude-docs",
                "actor_label": "Claude · Docs",
                "label": "Trace protocol docs",
                "status": "interrupted",
                "trace_id": "trace-docs",
                "context": {
                    "role": "Researcher",
                    "provider": "Claude Code",
                    "model": "claude-fixture",
                    "effort": "high",
                    "project": "clover",
                    "cwd": "/home/arnaud/dev/clover",
                    "tmux": "clover / docs / %34",
                    "agent_sid": "@dc-721b",
                },
            },
        ],
    }


@page_router.get("", response_class=HTMLResponse)
def timeline_page(request: Request):
    return templates.TemplateResponse(request, "timeline.html", {})


@api_router.get("/consent")
def timeline_consent():
    mode, source = capture_setting()
    return {
        "mode": mode,
        "source": source,
        "status": "pending" if mode == "ask" and hooks_drift() else "in-sync",
        "stores": [
            "event type and status",
            "timestamps and duration boundaries",
            "provider, model, effort, project, tmux, and stable agent identity",
            "live status from stable identity or the current tmux window",
        ],
        "never_stores": [
            "prompt or answer text",
            "complete commands or tool inputs",
            "tool results, model output, or secrets",
        ],
    }


@api_router.post("/consent")
def update_timeline_consent(update: ConsentUpdate):
    try:
        mode = set_capture_mode(update.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"mode": mode, "source": "config", "status": sync_hooks()}


@api_router.get("")
def timeline_data(
    since: datetime | None = None,
    until: datetime | None = None,
    grouping: str = Query("participants", pattern="^(participants|activity)$"),
    actor: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    project: str | None = None,
    provider: str | None = None,
    trace: str | None = None,
    cursor: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    state: str | None = None,
):
    if _fixture_enabled():
        scenario = state or "ready"
        if scenario == "empty":
            return {
                "state": "empty",
                "message": "No activity in this range.",
                "items": [],
            }
        if scenario == "no-results":
            return {
                "state": "no-results",
                "message": "No activity matches the current filters.",
                "items": [],
            }
        if scenario == "disabled":
            return {
                "state": "collector-disabled",
                "message": "Activity capture is off. Merlin stores metadata, never prompt or tool content.",
                "items": [],
            }
        if scenario == "disconnected":
            return {
                "state": "disconnected",
                "message": "Merlin could not refresh this local timeline. Existing history is unchanged.",
                "items": [],
            }
        if scenario == "loading":
            return {
                "state": "loading",
                "message": "Loading recent activity…",
                "items": [],
            }
        data = _fixture_data()
        if scenario == "malformed":
            data["skipped"] = 3
            data["flagged"] = 0
            data["anomalies"] = 3
        return data

    now = datetime.now(timezone.utc)
    until = _aware(until or now, "until")
    since = _aware(since or until - DEFAULT_RANGE, "since")
    if since > until:
        raise HTTPException(status_code=422, detail="since must not be after until")
    if until - since > MAX_RANGE:
        raise HTTPException(status_code=422, detail="range must not exceed 7 days")

    store = _store()
    try:
        read = store.read_range(since, until, cursor=cursor, limit=limit)
        span_context = store.read_span_context(since, until, limit=limit)
    except ActivityStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    context_events = span_context.events if cursor is None else []
    live_actors = _live_actors()
    live_agent_ids = live_actors[0] if live_actors is not None else None
    live_tmux_windows = live_actors[1] if live_actors is not None else None
    assembled = assemble(
        [*context_events, *read.events],
        now=now,
        live_agent_ids=live_agent_ids,
        live_tmux_windows=live_tmux_windows,
    )
    filters = {
        "actor": actor,
        "kind": kind,
        "status": status,
        "project": project,
        "provider": provider,
        "trace": trace,
    }
    known_span_ids = {
        span_item_id(trace_id, span_id)
        for trace_id, span_id in span_context.known_spans
    }
    incremental_boundaries = {
        update["id"]
        for update in assembled.updates
        if cursor is not None and update["id"] in known_span_ids
    }
    hidden_known_incremental_anomalies = sum(
        1
        for item in assembled.items
        if item.get("anomaly") == "finish-without-start"
        and span_item_id(item["trace_id"], item["span_id"]) in incremental_boundaries
    )
    visible_items = [
        item for item in assembled.items if _intersects_range(item, since, until)
    ]
    hidden_before_range = [
        item
        for item in assembled.items
        if item["phase"] == "span"
        and not _intersects_range(item, since, until)
        and datetime.fromisoformat(item["start_timestamp"].replace("Z", "+00:00"))
        < since
    ]
    items = [
        _relative(item, since)
        for item in visible_items
        if not (
            cursor is not None
            and item.get("anomaly") == "finish-without-start"
            and span_item_id(item["trace_id"], item["span_id"])
            in incremental_boundaries
        )
        and _matches(item, **filters)
    ]
    updates = [
        update for update in assembled.updates if _update_matches(update, **filters)
    ]
    has_visible_updates = bool(updates)
    updates.extend(
        _stale_span_update(item)
        for item in hidden_before_range
        if item.get("anomaly")
        in {"actor-not-live", "liveness-unknown", "liveness-unavailable"}
        and _matches(item, **filters)
    )
    tracks = tracks_for_items(items)
    has_filters = any(value is not None for value in filters.values())
    mode = capture_mode()
    if items or has_visible_updates:
        response_state = "ready"
        message = None
    elif has_filters:
        response_state = "no-results"
        message = "No activity matches the current filters."
    elif mode != "auto":
        response_state = "collector-disabled"
        message = "Activity capture is not enabled. Existing private history remains available."
    else:
        response_state = "empty"
        message = "No activity in this range."
    health = read_capture_health(store.directory)
    hidden_outside_range_anomalies = sum(
        1
        for item in assembled.items
        if item.get("anomaly") and not _intersects_range(item, since, until)
    )
    flagged = max(
        0,
        assembled.anomalies
        - hidden_known_incremental_anomalies
        - hidden_outside_range_anomalies,
    )
    return {
        "state": response_state,
        "message": message,
        "source": "activity-store",
        "range": {
            "start": _iso_query(since),
            "end": _iso_query(until),
            "now": _iso_query(now),
            "seconds": (until - since).total_seconds(),
        },
        "grouping": grouping,
        "tracks": tracks,
        "lanes": tracks[grouping],
        "items": items,
        "updates": updates,
        "cursor": read.cursor,
        "partial": read.partial or span_context.partial,
        "skipped": read.anomalies + span_context.anomalies,
        "flagged": flagged,
        "anomalies": read.anomalies + span_context.anomalies + flagged,
        "dropped": health["dropped"],
        "capture_health": health,
        "last_modified_ns": read.last_modified_ns,
    }
