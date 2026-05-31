"""Merlin Bot app — monitoring pages that plug into the Merlin dashboard.

Exports:
    merlin_app_router: FastAPI APIRouter with monitoring pages + API endpoints
    MERLIN_APP_NAV_ITEMS: Nav items to add to the sidebar
    MERLIN_APP_STATIC_DIR: Static files directory (None — uses root statics)
    BOT_START_TIME: Set by merlin_bot.py when the bot starts
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

import paths
from lib.event_log import read_events
from merlin_ext import make_templates

_SCRIPT_DIR = Path(__file__).parent.resolve()

ENGINE_LOG_PATH = paths.logs_dir() / "engine-log.jsonl"
RAW_SESSION_DIR = paths.logs_dir() / "raw-sessions"

# Bot start time — set by merlin_bot.py when it starts
BOT_START_TIME: datetime | None = None

templates = make_templates(_SCRIPT_DIR / "templates")

merlin_app_router = APIRouter()

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


def _validate_session_filename(filename: str) -> None:
    """Validate session filename to prevent path traversal."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not re.match(r"^[\w\-]+\.jsonl$", filename):
        raise HTTPException(status_code=400, detail="Invalid filename format")


def _parse_ts(event: dict) -> datetime | None:
    """Parse the timestamp field of an event."""
    try:
        return datetime.fromisoformat(event["timestamp"])
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@merlin_app_router.get("/bot", response_class=HTMLResponse)
def bot_page(request: Request):
    return templates.TemplateResponse(request, "bot.html", {"active_tab": "overview"})


@merlin_app_router.get("/bot/performance", response_class=HTMLResponse)
def bot_performance_page(request: Request):
    return templates.TemplateResponse(
        request, "bot.html", {"active_tab": "performance"}
    )


@merlin_app_router.get("/bot/logs", response_class=HTMLResponse)
def bot_logs_page(request: Request):
    return templates.TemplateResponse(request, "bot.html", {"active_tab": "logs"})


@merlin_app_router.get("/session/{filename}", response_class=HTMLResponse)
def session_page(request: Request, filename: str):
    _validate_session_filename(filename)
    session_path = RAW_SESSION_DIR / filename
    if not session_path.exists():
        raise HTTPException(status_code=404, detail="Session file not found")

    # Determine back link based on ?back= query parameter
    back_param = request.query_params.get("back", "bot")
    back_links = {
        "bot": ("/bot/logs", "Back to Bot Logs"),
        "cron": ("/cron", "Back to Cron"),
    }
    back_url, back_label = back_links.get(back_param, back_links["bot"])

    return templates.TemplateResponse(
        request,
        "session.html",
        {
            "filename": filename,
            "back_url": back_url,
            "back_label": back_label,
        },
    )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@merlin_app_router.get("/api/health")
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
        if not e.get("caller", "").startswith("cron-")
        if e.get("type") != "cron_dispatch" and e.get("type") != "cron_runner_crash"
    ]

    # Filter to Discord-only invocations (exclude cron callers)
    discord_invocations = [
        e for e in invocations if not e.get("caller", "").startswith("cron-")
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

    # Tunnel status
    try:
        from tunnel import get_public_url, get_status

        tunnel_url = get_public_url()
        tunnel_status = get_status()
    except ImportError:
        tunnel_url = None
        tunnel_status = "unavailable"

    return {
        "bot_start_time": BOT_START_TIME.isoformat() if BOT_START_TIME else last_ready,
        "invocations_today": len(today_invocations),
        "avg_duration_today": round(avg_duration, 2),
        "cost_today": round(cost_today, 2),
        "errors_24h": len(errors_24h),
        "last_error": last_error,
        "total_events": len(events),
        "tunnel_url": tunnel_url,
        "tunnel_status": tunnel_status,
    }


@merlin_app_router.get("/api/invocations")
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


@merlin_app_router.get("/api/events")
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


@merlin_app_router.get("/api/session/{filename}")
def api_session(filename: str):
    """Read a session JSONL file and return events as a JSON array."""
    _validate_session_filename(filename)
    session_path = RAW_SESSION_DIR / filename
    if not session_path.exists():
        raise HTTPException(status_code=404, detail="Session file not found")

    events = []
    for line in session_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return events


@merlin_app_router.get("/api/last-modified")
def api_last_modified():
    """Return the mtime of engine-log.jsonl for smart refresh."""
    if not ENGINE_LOG_PATH.exists():
        return {"mtime": None}
    mtime = ENGINE_LOG_PATH.stat().st_mtime
    return {"mtime": mtime}
