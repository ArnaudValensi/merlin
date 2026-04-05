"""
Hybrid cron execution log storage.

Individual log files per execution: ~/.merlin/cron-logs/{job-id}/{timestamp}.json
Metadata index (.history.json) stays small — no output field there.

Each log file contains: job_id, timestamp, exit_code, duration_seconds,
cost_usd, session_id, output, output_truncated.

Timestamp format in filenames: ISO-like but filesystem-safe — colons replaced
with hyphens, e.g. 2026-03-22T02-30-00+00-00.json
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import paths

logger = logging.getLogger("merlin.cron")

# Max output size in bytes before truncation
MAX_OUTPUT_BYTES = 102400  # 100KB

# Default max log files per job
DEFAULT_MAX_LOGS = 50


def _logs_base_dir() -> Path:
    """Return the cron logs base directory."""
    return paths.cron_logs_dir()


def _job_log_dir(job_id: str) -> Path:
    """Return the log directory for a specific job."""
    return _logs_base_dir() / job_id


def _timestamp_to_filename(timestamp: str) -> str:
    """Convert an ISO timestamp to a filesystem-safe filename.

    Replaces colons with hyphens: 2026-03-22T02:30:00+00:00 -> 2026-03-22T02-30-00+00-00.json
    """
    safe = timestamp.replace(":", "-")
    return f"{safe}.json"


def _filename_to_timestamp(filename: str) -> str:
    """Convert a filesystem-safe filename back to an ISO timestamp.

    Reverses the colon-to-hyphen replacement in the time portion only.
    E.g. 2026-03-22T02-30-00+00-00.json -> 2026-03-22T02:30:00+00:00
    """
    stem = filename.removesuffix(".json")

    # Find the 'T' separator between date and time
    t_idx = stem.find("T")
    if t_idx == -1:
        return stem  # no T found, return as-is

    date_part = stem[:t_idx]
    time_part = stem[t_idx + 1:]

    # The time portion has hyphens where colons should be.
    # Time format is like: 02-30-00+00-00 or 02-30-00Z
    # We need to restore colons for HH:MM:SS and timezone offset.

    # Split on + or - to find timezone boundary
    # First, handle the time-of-day portion (before timezone)
    # Find the timezone offset indicator (+ or - after the time digits)
    # The time starts as HH-MM-SS, then optionally +HH-MM or -HH-MM or Z

    # Strategy: we know the time part before tz is exactly HH-MM-SS (8 chars)
    # Replace first two hyphens with colons for HH:MM:SS
    if len(time_part) >= 8:
        time_hms = time_part[:8]
        tz_part = time_part[8:]

        # HH-MM-SS -> HH:MM:SS
        time_hms = time_hms.replace("-", ":", 2)

        # Timezone part: +HH-MM -> +HH:MM or -HH-MM -> -HH:MM
        if tz_part and (tz_part[0] in ("+", "-")) and len(tz_part) >= 6:
            tz_hm = tz_part[:6]
            tz_rest = tz_part[6:]
            # +HH-MM -> +HH:MM
            tz_hm = tz_hm[0] + tz_hm[1:].replace("-", ":", 1)
            tz_part = tz_hm + tz_rest

        return f"{date_part}T{time_hms}{tz_part}"

    return stem


def write_log(job_id: str, log_entry: dict) -> Path:
    """Write a single execution log file. Creates dirs. Returns path.

    If the output field exceeds MAX_OUTPUT_BYTES, it is truncated
    and output_truncated is set to True.
    """
    job_dir = _job_log_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    # Handle output truncation
    output = log_entry.get("output", "")
    if isinstance(output, str) and len(output.encode("utf-8", errors="replace")) > MAX_OUTPUT_BYTES:
        # Truncate to MAX_OUTPUT_BYTES at character boundary
        encoded = output.encode("utf-8", errors="replace")
        truncated = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        log_entry = {**log_entry, "output": truncated, "output_truncated": True}
    elif "output_truncated" not in log_entry:
        log_entry = {**log_entry, "output_truncated": False}

    timestamp = log_entry.get("timestamp", "")
    filename = _timestamp_to_filename(timestamp)
    log_path = job_dir / filename

    log_path.write_text(json.dumps(log_entry, indent=2))
    return log_path


def list_logs(job_id: str, limit: int = 50) -> list[dict]:
    """List log files sorted newest-first. Returns metadata (no output field)."""
    job_dir = _job_log_dir(job_id)
    if not job_dir.exists():
        return []

    log_files = sorted(job_dir.glob("*.json"), reverse=True)

    if limit:
        log_files = log_files[:limit]

    results = []
    for path in log_files:
        try:
            data = json.loads(path.read_text())
            # Strip the output field for list view — keep it lightweight
            data.pop("output", None)
            results.append(data)
        except (json.JSONDecodeError, OSError):
            continue

    return results


def read_log(job_id: str, timestamp: str) -> dict | None:
    """Read a specific log file by timestamp string. Returns None if not found."""
    job_dir = _job_log_dir(job_id)
    filename = _timestamp_to_filename(timestamp)
    log_path = job_dir / filename

    if not log_path.exists():
        return None

    try:
        return json.loads(log_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def cleanup_logs(job_id: str, max_logs: int = DEFAULT_MAX_LOGS) -> int:
    """Delete oldest logs if count exceeds max. Returns number deleted."""
    job_dir = _job_log_dir(job_id)
    if not job_dir.exists():
        return 0

    log_files = sorted(job_dir.glob("*.json"))  # oldest first

    if len(log_files) <= max_logs:
        return 0

    to_delete = log_files[: len(log_files) - max_logs]
    for path in to_delete:
        try:
            path.unlink()
        except OSError:
            pass

    return len(to_delete)


def delete_logs(job_id: str) -> None:
    """Remove entire job log directory."""
    job_dir = _job_log_dir(job_id)
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
