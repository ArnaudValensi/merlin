"""
Engine event logger — writes JSONL events to logs/engine-log.jsonl.

Single source of truth for the monitoring dashboard. Each line is a JSON
object with a "type" field and an ISO 8601 UTC timestamp.

Event types:
  - invocation:    Claude Code call (from lib/claude.py)
  - bot_event:     Discord bot lifecycle (from merlin_bot.py)
  - cron_dispatch: Cron job execution (from cron/runner.py)

Usage:
    from structured_log import log_event

    log_event("invocation", caller="discord", duration=12.5, exit_code=0)
    log_event("bot_event", event="ready", details="Bot started")
    log_event("cron_dispatch", job_id="weather", event="completed", duration=30.1)
"""

import json
import logging
import threading
from datetime import datetime, timezone

import paths

ENGINE_LOG_PATH = paths.logs_dir() / "engine-log.jsonl"
RAW_SESSION_DIR = paths.logs_dir() / "raw-sessions"

_write_lock = threading.Lock()
_logger = logging.getLogger("merlin.structured_log")

# Retention settings
ENGINE_LOG_RETENTION_DAYS = 180
RAW_SESSION_RETENTION_DAYS = 90


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
        ENGINE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(ENGINE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)


def cleanup_old_logs() -> None:
    """Remove old engine log entries and raw session files.

    Called at startup to keep disk usage bounded.
    - engine-log.jsonl: remove entries older than ENGINE_LOG_RETENTION_DAYS
    - raw-sessions/: remove files older than RAW_SESSION_RETENTION_DAYS
    """
    import time

    now = time.time()

    # Clean raw session files by mtime
    if RAW_SESSION_DIR.is_dir():
        cutoff = now - RAW_SESSION_RETENTION_DAYS * 86400
        removed = 0
        for f in RAW_SESSION_DIR.iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
        if removed:
            _logger.info(
                "Cleaned up %d raw session files older than %d days",
                removed,
                RAW_SESSION_RETENTION_DAYS,
            )

    # Clean engine log by filtering out old entries
    if ENGINE_LOG_PATH.exists():
        from datetime import timedelta

        cutoff_str = (
            datetime.now(tz=timezone.utc) - timedelta(days=ENGINE_LOG_RETENTION_DAYS)
        ).isoformat()

        try:
            with _write_lock:
                lines = ENGINE_LOG_PATH.read_text(encoding="utf-8").splitlines()
                kept = []
                removed = 0
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                        ts = event.get("timestamp", "")
                        if ts >= cutoff_str:
                            kept.append(line)
                        else:
                            removed += 1
                    except (json.JSONDecodeError, KeyError):
                        kept.append(line)  # keep unparseable lines

                if removed:
                    ENGINE_LOG_PATH.write_text(
                        "\n".join(kept) + "\n" if kept else "", encoding="utf-8"
                    )

            if removed:
                _logger.info(
                    "Cleaned up %d engine log entries older than %d days",
                    removed,
                    ENGINE_LOG_RETENTION_DAYS,
                )
        except OSError as e:
            _logger.warning("Failed to clean engine log: %s", e)
