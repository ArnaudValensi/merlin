# /// script
# dependencies = []
# ///
"""
State and history tracking for cron jobs.

- .state/{job_id}: per-job last run timestamp (one file per job)
- .history.json: run history with rolling limit of 100 per job
- .locks/{job_id}.lock: per-job execution locks (flock-based)
- .locks/_history.lock: history file write lock
"""

import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("cron-runner")

import paths

CRON_JOBS_DIR = paths.cron_jobs_dir()
STATE_DIR = CRON_JOBS_DIR / ".state"
LOCKS_DIR = CRON_JOBS_DIR / ".locks"
HISTORY_FILE = CRON_JOBS_DIR / ".history.json"
HISTORY_LIMIT = 100  # Max runs to keep per job

# ---------------------------------------------------------------------------
# State (last run time per job) — per-job files
# ---------------------------------------------------------------------------


def get_last_run(job_id: str) -> datetime | None:
    """Get the last run time for a job, or None if never run."""
    state_file = STATE_DIR / job_id
    if not state_file.exists():
        return None
    try:
        text = state_file.read_text().strip()
        if not text:
            return None
        return datetime.fromisoformat(text)
    except (ValueError, OSError):
        return None


def set_last_run(job_id: str, timestamp: datetime | None = None) -> None:
    """Set the last run time for a job. Defaults to now (UTC)."""
    if timestamp is None:
        timestamp = datetime.now(tz=timezone.utc)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / job_id).write_text(timestamp.isoformat())


# ---------------------------------------------------------------------------
# Per-job locking (flock-based)
# ---------------------------------------------------------------------------


def acquire_job_lock(job_id: str) -> object | None:
    """Try to acquire an exclusive lock for a job (non-blocking).

    Returns the open file object if lock acquired (caller must keep it alive),
    or None if the job is already locked by another process/thread.
    The lock is released when the file object is closed or garbage collected.
    """
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCKS_DIR / f"{job_id}.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except (BlockingIOError, OSError):
        lock_file.close()
        return None


def release_job_lock(lock_file: object) -> None:
    """Release a job lock acquired with acquire_job_lock()."""
    try:
        lock_file.close()
    except (OSError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# History (run log per job)
# ---------------------------------------------------------------------------


def _acquire_history_lock():
    """Acquire exclusive lock for history file writes. Returns file object."""
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCKS_DIR / "_history.lock"
    lock_file = open(lock_path, "w")
    fcntl.flock(lock_file, fcntl.LOCK_EX)  # blocking — wait for lock
    return lock_file


def read_history() -> dict[str, list[dict]]:
    """Read the history file. Returns {job_id: [run_entry, ...]}."""
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def write_history(history: dict[str, list[dict]]) -> None:
    """Write the history file."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def append_history(
    job_id: str,
    exit_code: int,
    duration: float,
    session_id: str | None = None,
    timestamp: datetime | None = None,
    cost_usd: float | None = None,
) -> None:
    """Append a run entry to the history for a job. Enforces rolling limit.

    Thread-safe and process-safe via flock on _history.lock.
    """
    if timestamp is None:
        timestamp = datetime.now(tz=timezone.utc)

    entry = {
        "timestamp": timestamp.isoformat(),
        "exit_code": exit_code,
        "duration": round(duration, 2),
        "session_id": session_id,
        "cost_usd": round(cost_usd, 6) if cost_usd is not None else None,
    }

    lock_file = _acquire_history_lock()
    try:
        history = read_history()
        if job_id not in history:
            history[job_id] = []

        history[job_id].append(entry)

        # Enforce rolling limit
        if len(history[job_id]) > HISTORY_LIMIT:
            history[job_id] = history[job_id][-HISTORY_LIMIT:]

        write_history(history)
    finally:
        lock_file.close()


def get_history(job_id: str, limit: int | None = None) -> list[dict]:
    """Get run history for a job, most recent first. Optional limit."""
    history = read_history()
    runs = history.get(job_id, [])
    # Return most recent first
    runs = list(reversed(runs))
    if limit:
        runs = runs[:limit]
    return runs


def get_all_history(limit_per_job: int | None = None) -> dict[str, list[dict]]:
    """Get run history for all jobs, most recent first per job."""
    history = read_history()
    result = {}
    for job_id, runs in history.items():
        runs = list(reversed(runs))
        if limit_per_job:
            runs = runs[:limit_per_job]
        result[job_id] = runs
    return result
