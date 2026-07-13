"""Session viewer — renders a single agent-run transcript.

Core module. The raw session files under ``logs/raw-sessions/`` are written by
``lib/`` for every invocation (jobs, bot, terminal), so viewing one is shared
infrastructure, not a bot feature — this used to live in ``merlin-bot`` and a
core page (jobs) linked into it, which broke when the bot was disabled. As a
core module it is always available.

The framework mounts ``api_router`` at ``/api/session`` and ``page_router`` at
``/session`` (``URL_SLUG = "session"``). The viewer is fully caller-agnostic:
it takes a filename and shows that one run. The only per-caller trace is the
``?back=`` query param, which just picks where the back link points.
"""

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import paths
from merlin_ext import make_templates

URL_SLUG = "session"

SESSIONS_DIR = Path(__file__).parent.resolve()
SESSIONS_TEMPLATES_DIR = SESSIONS_DIR / "templates"
RAW_SESSION_DIR = paths.logs_dir() / "raw-sessions"

templates = make_templates(SESSIONS_TEMPLATES_DIR)

api_router = APIRouter()
page_router = APIRouter()


def _validate_session_filename(filename: str) -> None:
    """Validate session filename to prevent path traversal."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not re.match(r"^[\w\-]+\.jsonl$", filename):
        raise HTTPException(status_code=400, detail="Invalid filename format")


@page_router.get("/{filename}", response_class=HTMLResponse)
def session_page(request: Request, filename: str):
    _validate_session_filename(filename)
    session_path = RAW_SESSION_DIR / filename
    if not session_path.exists():
        raise HTTPException(status_code=404, detail="Session file not found")

    # Determine back link based on ?back= query parameter. The caller that
    # linked here (bot logs, jobs) names itself so the "Back" button returns
    # to the right list; the transcript itself is identical regardless.
    back_param = request.query_params.get("back", "bot")
    back_links = {
        "bot": ("/bot/logs", "Back to Bot Logs"),
        "jobs": ("/jobs", "Back to Jobs"),
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


@api_router.get("/{filename}")
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
