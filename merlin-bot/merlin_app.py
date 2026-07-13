"""Merlin Bot app — monitoring pages that plug into the Merlin dashboard.

Exports:
    api_router: API endpoints, mounted by the framework at /api/bot
    page_router: monitoring pages, mounted by the framework at /bot
    URL_SLUG: "bot" — the /bot + /api/bot namespace
    MERLIN_APP_NAV_ITEMS: Nav items to add to the sidebar
    MERLIN_APP_STATIC_DIR: Static files directory (None — uses root statics)
    BOT_START_TIME: Set by merlin_bot.py when the bot starts
"""

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

import paths
from lib.event_log import read_events
from merlin_ext import make_templates

_SCRIPT_DIR = Path(__file__).parent.resolve()

ENGINE_LOG_PATH = paths.logs_dir() / "engine-log.jsonl"

# Bot start time — set by merlin_bot.py when it starts
BOT_START_TIME: datetime | None = None

templates = make_templates(_SCRIPT_DIR / "templates")

# URL_SLUG="bot" — the module id is "merlin-bot" but its pages live under
# /bot. api_router → /api/bot, page_router → /bot (both authed).
URL_SLUG = "bot"

api_router = APIRouter()
page_router = APIRouter()

# No static dir — monitoring pages use the root dashboard.css/js
MERLIN_APP_STATIC_DIR = None

# Nav items for the sidebar
_SVG = 'width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
MERLIN_APP_NAV_ITEMS = [
    {
        "url": "/bot",
        "icon": f'<svg {_SVG}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
        "label": "Bot",
    },
]


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _read_event_dicts(
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Adapt the shared typed reader to the plain dicts these pages expect.

    The bot pages predate the typed models and serialize events verbatim.
    ``model_dump(exclude_unset=True)`` reproduces the exact source shape — the
    declared fields that were actually present plus any extra fields — so the
    JSON these endpoints emit is byte-for-byte unchanged.
    """
    return [
        e.model_dump(exclude_unset=True)
        for e in read_events(event_type=event_type, since=since, until=until)
    ]


def _parse_ts(event: dict) -> datetime | None:
    """Parse the timestamp field of an event."""
    try:
        return datetime.fromisoformat(event["timestamp"])
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@page_router.get("", response_class=HTMLResponse)
def bot_page(request: Request):
    return templates.TemplateResponse(request, "bot.html", {"active_tab": "overview"})


@page_router.get("/performance", response_class=HTMLResponse)
def bot_performance_page(request: Request):
    return templates.TemplateResponse(
        request, "bot.html", {"active_tab": "performance"}
    )


@page_router.get("/logs", response_class=HTMLResponse)
def bot_logs_page(request: Request):
    return templates.TemplateResponse(request, "bot.html", {"active_tab": "logs"})


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@api_router.get("/health")
def api_health():
    """System health summary."""
    now = datetime.now(tz=timezone.utc)
    events = _read_event_dicts()
    invocations = [e for e in events if e["type"] == "invocation"]
    errors_24h = [
        e
        for e in events
        if e.get("exit_code", 0) != 0 or e.get("event") == "error"
        if (ts := _parse_ts(e)) is not None and (now - ts).total_seconds() < 86400
        if not e.get("caller", "").startswith("job-")
        if e.get("type") != "job_dispatch" and e.get("type") != "job_runner_crash"
    ]

    # Filter to Discord-only invocations (exclude job callers)
    discord_invocations = [
        e for e in invocations if not e.get("caller", "").startswith("job-")
    ]
    today_invocations = [
        e
        for e in discord_invocations
        if (ts := _parse_ts(e)) is not None and ts.date() == now.date()
    ]

    avg_duration = 0.0
    if today_invocations:
        durations = [e.get("duration", 0) for e in today_invocations]
        avg_duration = sum(durations) / len(durations)

    cost_today = sum(e.get("cost_usd", 0) or 0 for e in today_invocations)

    # Last bot event
    bot_events = [e for e in events if e["type"] == "bot_event"]
    last_ready = None
    for e in reversed(bot_events):
        if e.get("event") == "ready":
            last_ready = e["timestamp"]
            break

    # Last error
    last_error = None
    if errors_24h:
        last_error = errors_24h[-1]

    return {
        "bot_start_time": BOT_START_TIME.isoformat() if BOT_START_TIME else last_ready,
        "invocations_today": len(today_invocations),
        "avg_duration_today": round(avg_duration, 2),
        "cost_today": round(cost_today, 2),
        "errors_24h": len(errors_24h),
        "last_error": last_error,
        "total_events": len(events),
    }


@api_router.get("/invocations")
def api_invocations(
    since: str | None = Query(None, description="ISO 8601 start time"),
    until: str | None = Query(None, description="ISO 8601 end time"),
    caller: str | None = Query(None, description="Filter by caller"),
):
    """List invocation events."""
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    events = _read_event_dicts("invocation", since=since_dt, until=until_dt)

    if caller:
        events = [e for e in events if e.get("caller") == caller]

    return events


@api_router.get("/events")
def api_events(
    event_type: str | None = Query(
        None, alias="type", description="Filter by event type"
    ),
    since: str | None = Query(None, description="ISO 8601 start time"),
    until: str | None = Query(None, description="ISO 8601 end time"),
    status: str | None = Query(None, description="Filter: success, error, all"),
):
    """List all events."""
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    events = _read_event_dicts(event_type=event_type, since=since_dt, until=until_dt)

    if status == "error":
        events = [
            e for e in events if e.get("exit_code", 0) != 0 or e.get("event") == "error"
        ]
    elif status == "success":
        events = [
            e
            for e in events
            if e.get("exit_code", 0) == 0 and e.get("event") != "error"
        ]

    return events


@api_router.get("/last-modified")
def api_last_modified():
    """Return the mtime of engine-log.jsonl for smart refresh."""
    if not ENGINE_LOG_PATH.exists():
        return {"mtime": None}
    mtime = ENGINE_LOG_PATH.stat().st_mtime
    return {"mtime": mtime}
