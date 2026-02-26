# /// script
# dependencies = []
# ///
"""
Structured event logger — writes JSONL events to logs/structured.jsonl.

Single source of truth for the monitoring dashboard. Each line is a JSON
object with a "type" field and an ISO 8601 UTC timestamp.

Event types:
  - invocation:    Claude Code call (from claude_wrapper.py)
  - bot_event:     Discord bot lifecycle (from merlin_bot.py)
  - cron_dispatch: Cron job execution (from cron_runner.py)

Usage:
    from structured_log import log_event

    log_event("invocation", caller="discord", duration=12.5, exit_code=0)
    log_event("bot_event", event="ready", details="Bot started")
    log_event("cron_dispatch", job_id="weather", event="completed", duration=30.1)
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import paths

STRUCTURED_LOG_PATH = paths.logs_dir() / "structured.jsonl"

_write_lock = threading.Lock()


def log_event(event_type: str, **fields) -> None:
    """Append a structured JSON event to the log file.

    Args:
        event_type: Event type (invocation, bot_event, cron_dispatch).
        **fields: Type-specific fields to include in the event.
    """
    event = {
        "type": event_type,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        **fields,
    }

    line = json.dumps(event, default=str) + "\n"

    with _write_lock:
        STRUCTURED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STRUCTURED_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
