"""
Event logger — writes JSONL events to logs/events.jsonl.

Each line is a JSON object with a "type" field and an ISO 8601 UTC
timestamp. The typed reader is lib/event_log.py.

Event types:
  - app_started / app_stopped: server lifecycle (main.py)
  - job_runner_crash: the job runner exited non-zero (job/__init__.py)
  - webhook_request: a webhook hit the front desk (webhooks/)
  - attention_routed: a notification routed to the push sender
  - push_failed / push_removed: what Web Push reported for a device
  - job_dispatch: a job run started, completed or failed (job/runner.py)

Usage:
    from structured_log import log_event

    log_event("job_runner_crash", exit_code=1, stderr="...")
"""

import json
import logging
import shutil
import threading
from datetime import datetime, timezone

import paths

EVENT_LOG_PATH = paths.logs_dir() / "events.jsonl"

_write_lock = threading.Lock()
_logger = logging.getLogger("merlin.structured_log")

# Retention settings
EVENT_LOG_RETENTION_DAYS = 180


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
        EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)


# What releases with an agent engine and a Discord bot left behind, all of it
# written by Merlin and read by nothing now.
_OLD_EVENT_LOG = "engine-log.jsonl"
_OLD_EVENT_TYPES = frozenset({"bot_event"})
_OLD_LOG_FILES = ("cron_runner.log",)
_OLD_LOG_DIRS = ("raw-sessions", "claude")


def retire_old_logs() -> None:
    """Remove the logs of the old model: the event log moves to its new name
    without the event types nothing writes any more, and the old session
    transcripts and bot logs go. Each step stands alone, so one that fails is
    retried at the next startup and stops nothing."""
    logs = EVENT_LOG_PATH.parent
    old = logs / _OLD_EVENT_LOG
    try:
        if old.exists():
            kept = [
                line
                for line in old.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines(keepends=True)
                if _event_type(line) not in _OLD_EVENT_TYPES
            ]
            with _write_lock, open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
                f.writelines(kept)
            old.unlink()
            _logger.info("Event log moved to %s", EVENT_LOG_PATH.name)
    except OSError as e:
        _logger.warning("Failed to move the old event log: %s", e)
    for name in _OLD_LOG_FILES:
        try:
            (logs / name).unlink(missing_ok=True)
        except OSError as e:
            _logger.warning("Failed to remove the old log %s: %s", name, e)
    for directory in (
        *(logs / name for name in _OLD_LOG_DIRS),
        paths.data_dir() / "sessions",
    ):
        if directory.is_dir() and not directory.is_symlink():
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                _logger.info("Removed the old logs in %s", directory)


def _event_type(line: str) -> str | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event.get("type") if isinstance(event, dict) else None


def cleanup_old_logs() -> None:
    """Remove event log entries older than EVENT_LOG_RETENTION_DAYS, and the
    logs of the old model. Called at startup to keep disk usage bounded."""
    retire_old_logs()
    if EVENT_LOG_PATH.exists():
        from datetime import timedelta

        cutoff_str = (
            datetime.now(tz=timezone.utc) - timedelta(days=EVENT_LOG_RETENTION_DAYS)
        ).isoformat()

        try:
            with _write_lock:
                lines = EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines()
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
                    EVENT_LOG_PATH.write_text(
                        "\n".join(kept) + "\n" if kept else "", encoding="utf-8"
                    )

            if removed:
                _logger.info(
                    "Cleaned up %d event log entries older than %d days",
                    removed,
                    EVENT_LOG_RETENTION_DAYS,
                )
        except OSError as e:
            _logger.warning("Failed to clean event log: %s", e)
