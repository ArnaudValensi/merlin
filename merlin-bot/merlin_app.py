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
from fastapi.templating import Jinja2Templates

import paths

_SCRIPT_DIR = Path(__file__).parent.resolve()

STRUCTURED_LOG_PATH = paths.logs_dir() / "structured.jsonl"
SESSION_DIR = paths.logs_dir() / "sessions"
CRON_JOBS_DIR = paths.cron_jobs_dir()

# Bot start time — set by merlin_bot.py when it starts
BOT_START_TIME: datetime | None = None

# Search bot templates first (overview, performance, logs, session),
# then root templates for base.html
templates = Jinja2Templates(
    directory=[str(_SCRIPT_DIR / "templates"), str(paths.app_dir() / "templates")]
)

merlin_app_router = APIRouter()

# No static dir — monitoring pages use the root dashboard.css/js
MERLIN_APP_STATIC_DIR = None

# Nav items for the sidebar
MERLIN_APP_NAV_ITEMS = [
    {"url": "/overview", "icon": "&#9673;", "label": "Overview"},
    {"url": "/performance", "icon": "&#9632;", "label": "Performance"},
    {"url": "/logs", "icon": "&#9776;", "label": "Logs"},
]


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def read_events(
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Read events from structured.jsonl, optionally filtered."""
    if not STRUCTURED_LOG_PATH.exists():
        return []

    events = []
    for line in STRUCTURED_LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event_type and event.get("type") != event_type:
            continue

        if since or until:
            try:
                ts = datetime.fromisoformat(event["timestamp"])
            except (KeyError, ValueError):
                continue
            if since and ts < since:
                continue
            if until and ts > until:
                continue

        events.append(event)

    return events


def read_cron_jobs() -> dict[str, dict]:
    """Read all cron job definitions."""
    jobs = {}
    if not CRON_JOBS_DIR.exists():
        return jobs
    for path in CRON_JOBS_DIR.glob("*.json"):
        if path.name.startswith(".") or path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text())
            jobs[path.stem] = data
        except (json.JSONDecodeError, OSError):
            continue
    return jobs


def read_cron_history() -> dict[str, list[dict]]:
    """Read cron job run history."""
    history_file = CRON_JOBS_DIR / ".history.json"
    if not history_file.exists():
        return {}
    try:
        return json.loads(history_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _validate_session_filename(filename: str) -> None:
    """Validate session filename to prevent path traversal."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not re.match(r'^[\w\-]+\.jsonl$', filename):
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


@merlin_app_router.get("/overview", response_class=HTMLResponse)
def overview_page(request: Request):
    return templates.TemplateResponse("overview.html", {"request": request})


@merlin_app_router.get("/performance", response_class=HTMLResponse)
def performance_page(request: Request):
    return templates.TemplateResponse("performance.html", {"request": request})


@merlin_app_router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    return templates.TemplateResponse("logs.html", {"request": request})


@merlin_app_router.get("/session/{filename}", response_class=HTMLResponse)
def session_page(request: Request, filename: str):
    _validate_session_filename(filename)
    session_path = SESSION_DIR / filename
    if not session_path.exists():
        raise HTTPException(status_code=404, detail="Session file not found")
    return templates.TemplateResponse("session.html", {
        "request": request,
        "filename": filename,
    })


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@merlin_app_router.get("/api/health")
def api_health():
    """System health summary."""
    now = datetime.now(tz=timezone.utc)
    events = read_events()
    invocations = [e for e in events if e["type"] == "invocation"]
    errors_24h = [
        e for e in events
        if e.get("exit_code", 0) != 0 or e.get("event") == "error"
        if _parse_ts(e) and (now - _parse_ts(e)).total_seconds() < 86400
    ]

    today_invocations = [
        e for e in invocations
        if _parse_ts(e) and _parse_ts(e).date() == now.date()
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

    # Cron status
    cron_dispatches = [e for e in events if e["type"] == "cron_dispatch"]
    last_cron = cron_dispatches[-1]["timestamp"] if cron_dispatches else None
    cron_failures_24h = [
        e for e in cron_dispatches
        if e.get("event") == "failed"
        if _parse_ts(e) and (now - _parse_ts(e)).total_seconds() < 86400
    ]

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
        "last_cron_dispatch": last_cron,
        "cron_failures_24h": len(cron_failures_24h),
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

    events = read_events("invocation", since=since_dt, until=until_dt)

    if caller:
        events = [e for e in events if e.get("caller") == caller]

    return events


@merlin_app_router.get("/api/events")
def api_events(
    event_type: str | None = Query(None, alias="type", description="Filter by event type"),
    since: str | None = Query(None, description="ISO 8601 start time"),
    until: str | None = Query(None, description="ISO 8601 end time"),
    status: str | None = Query(None, description="Filter: success, error, all"),
):
    """List all events."""
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    events = read_events(event_type=event_type, since=since_dt, until=until_dt)

    if status == "error":
        events = [e for e in events if e.get("exit_code", 0) != 0 or e.get("event") == "error"]
    elif status == "success":
        events = [e for e in events if e.get("exit_code", 0) == 0 and e.get("event") != "error"]

    return events


@merlin_app_router.get("/api/jobs")
def api_jobs():
    """List cron jobs with their definitions and recent history."""
    jobs = read_cron_jobs()
    history = read_cron_history()

    result = {}
    for job_id, job in jobs.items():
        runs = history.get(job_id, [])
        runs = list(reversed(runs))[:20]
        result[job_id] = {
            **job,
            "recent_runs": runs,
        }

    return result


@merlin_app_router.get("/api/session/{filename}")
def api_session(filename: str):
    """Read a session JSONL file and return events as a JSON array."""
    _validate_session_filename(filename)
    session_path = SESSION_DIR / filename
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
    """Return the mtime of structured.jsonl for smart refresh."""
    if not STRUCTURED_LOG_PATH.exists():
        return {"mtime": None}
    mtime = STRUCTURED_LOG_PATH.stat().st_mtime
    return {"mtime": mtime}
