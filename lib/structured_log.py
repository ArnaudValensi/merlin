"""
Event logger — writes JSONL events to logs/engine-log.jsonl.

Each line is a JSON object with a "type" field and an ISO 8601 UTC
timestamp. The typed reader is lib/event_log.py.

Event types:
  - app_started / app_stopped: server lifecycle (main.py)
  - job_runner_crash: the job runner exited non-zero (job/__init__.py)
  - webhook_request: a webhook hit the front desk (webhooks/)
  - attention_routed: a notification routed to the push sender

Usage:
    from structured_log import log_event

    log_event("job_runner_crash", exit_code=1, stderr="...")
"""

import json
import logging
import threading
from datetime import datetime, timezone

import paths

ENGINE_LOG_PATH = paths.logs_dir() / "engine-log.jsonl"

_write_lock = threading.Lock()
_logger = logging.getLogger("merlin.structured_log")

# Retention settings
ENGINE_LOG_RETENTION_DAYS = 180


def log_event(event_type: str, **fields) -> None:
    """Append a structured JSON event to the log file.

    Args:
        event_type: Event type (see the module docstring).
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
    """Remove engine log entries older than ENGINE_LOG_RETENTION_DAYS.

    Called at startup to keep disk usage bounded. Nothing else under
    logs/ is touched: a ``raw-sessions/`` directory left by an older
    release (the removed agent engine wrote it) is the user's to keep.
    """
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
