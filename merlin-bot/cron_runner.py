# /// script
# dependencies = [
#   "croniter",
#   "python-dotenv",
# ]
# ///
"""
Cron job dispatcher — runs every minute via the scheduler in merlin_bot.py.

Reads job files from cron-jobs/, checks if each is due, and executes via Claude.
Jobs run in parallel (ThreadPoolExecutor) with per-job flock to prevent double dispatch.
"""

import json
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))  # project root for paths module

from croniter import croniter
from dotenv import load_dotenv

import paths
from claude_wrapper import invoke_claude
from cron_state import (
    acquire_job_lock,
    append_history,
    get_last_run,
    release_job_lock,
    set_last_run,
)
from structured_log import log_event

_SCRIPT_DIR = Path(__file__).parent.resolve()

load_dotenv(paths.bot_config_path())

# Timezone for interpreting cron schedules. Loaded lazily in _validate_config().
CRON_TZ: ZoneInfo | None = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CRON_JOBS_DIR = paths.cron_jobs_dir()
LOG_DIR = paths.logs_dir()
LOG_FILE = LOG_DIR / "cron_runner.log"

# Default max turns per job execution (0 = unlimited)
DEFAULT_MAX_TURNS = 0

# Default grace period for staleness check (minutes).
# Jobs that missed their schedule by more than this are skipped.
DEFAULT_GRACE_MINUTES = 15

# Max parallel job executions
MAX_WORKERS = 6

# Report mode prompt suffixes
REPORT_MODE_PROMPTS = {
    "silent": (
        "\n\n[Cron job instruction: Only send a message to Discord if you have "
        "something noteworthy to report. If nothing to report, do nothing.]"
    ),
    "always": (
        "\n\n[Cron job instruction: Send your findings to Discord even if "
        "there's nothing new.]"
    ),
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("cron-runner")
logger.setLevel(logging.DEBUG)

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
)
logger.addHandler(_console_handler)


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------


def session_id_for_job(job_id: str) -> str:
    """Derive a deterministic UUID session ID from a job ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"cron-job-{job_id}"))


def load_job(path: Path) -> dict | None:
    """Load and validate a job file. Returns None if invalid."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load job %s: %s", path.name, e)
        return None

    # Validate required fields
    required = ["schedule", "prompt", "channel"]
    missing = [f for f in required if f not in data]
    if missing:
        logger.warning("Job %s missing required fields: %s", path.name, missing)
        return None

    # Validate cron expression
    try:
        croniter(data["schedule"])
    except (KeyError, ValueError) as e:
        logger.warning("Job %s has invalid schedule: %s", path.name, e)
        return None

    return data


def load_all_jobs() -> dict[str, dict]:
    """Load all valid job files. Returns {job_id: job_data}."""
    jobs = {}
    if not CRON_JOBS_DIR.exists():
        return jobs

    for path in CRON_JOBS_DIR.glob("*.json"):
        # Skip dotfiles and templates
        if path.name.startswith(".") or path.name.startswith("_"):
            continue

        job_id = path.stem  # Filename without extension
        job = load_job(path)
        if job:
            jobs[job_id] = job

    return jobs


def _now() -> datetime:
    """Current time in the configured timezone (or system local if unset)."""
    if CRON_TZ:
        return datetime.now(tz=CRON_TZ)
    return datetime.now(tz=timezone.utc)


def is_job_due(job_id: str, schedule: str, now: datetime,
               grace_minutes: int = DEFAULT_GRACE_MINUTES) -> bool:
    """Check if a job is due to run based on schedule and last run time.

    Includes staleness window and never-seen guard:
    - Never-seen jobs: state initialized to now, returns False (wait for next schedule)
    - Stale jobs (missed by >grace_minutes): state advanced to now, returns False
    - Otherwise: standard croniter check
    """
    last_run = get_last_run(job_id)

    if last_run is None:
        # Never-seen guard: initialize state, don't run immediately
        set_last_run(job_id, now)
        cron = croniter(schedule, now)
        next_time = cron.get_next(datetime)
        logger.info("New job %s registered, first run at %s", job_id, next_time.isoformat())
        return False

    # Normalize to configured timezone so croniter interprets the schedule
    # in the right timezone (e.g. "30 7" = 7:30 local, not 7:30 UTC)
    if CRON_TZ:
        last_run = last_run.astimezone(CRON_TZ)
        now = now.astimezone(CRON_TZ)

    # Get the next scheduled time after last run
    cron = croniter(schedule, last_run)
    next_run = cron.get_next(datetime)

    if next_run > now:
        return False  # Not due yet

    # Staleness check: if the job missed its window by too much, skip it
    staleness_seconds = (now - next_run).total_seconds()
    grace_seconds = grace_minutes * 60

    if staleness_seconds > grace_seconds:
        logger.warning(
            "Job %s missed its window by %.0f min (grace=%d min), skipping — advancing state",
            job_id, staleness_seconds / 60, grace_minutes,
        )
        set_last_run(job_id, now)
        return False

    return True


def build_prompt(job: dict) -> str:
    """Build the full prompt for a job, including report_mode instruction."""
    prompt = job["prompt"]
    report_mode = job.get("report_mode", "always")
    suffix = REPORT_MODE_PROMPTS.get(report_mode, REPORT_MODE_PROMPTS["always"])
    return prompt + suffix


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_job(job_id: str, job: dict) -> None:
    """Execute a single cron job. Acquires per-job lock to prevent double dispatch."""
    # Acquire per-job lock (non-blocking)
    lock = acquire_job_lock(job_id)
    if lock is None:
        logger.warning("Job %s already running (locked), skipping", job_id)
        return

    try:
        _execute_job(job_id, job)
    finally:
        release_job_lock(lock)


def _execute_job(job_id: str, job: dict) -> None:
    """Execute a job (internal — assumes lock is held)."""
    channel = job["channel"]
    max_turns_cfg = job.get("max_turns", DEFAULT_MAX_TURNS)
    # 0 means unlimited — pass None to wrapper so --max-turns flag is omitted
    max_turns = max_turns_cfg if max_turns_cfg > 0 else None
    ephemeral = job.get("ephemeral", True)

    # Ephemeral jobs (default) get a fresh UUID each time — no session continuity.
    # Cross-run context should use the memory system (KB, logs), not session history.
    # Set "ephemeral": false to opt into persistent sessions (costs grow per run).
    if ephemeral:
        session = str(uuid.uuid4())
    else:
        session = session_id_for_job(job_id)

    prompt = build_prompt(job)

    logger.info("Running job %s (channel=%s, max_turns=%s, ephemeral=%s)", job_id, channel, max_turns or "unlimited", ephemeral)
    log_event("cron_dispatch", job_id=job_id, event="started", duration=0, exit_code=0)

    # Mark as running BEFORE execution to prevent re-dispatch by concurrent schedulers
    set_last_run(job_id, _now())

    # Add channel context to prompt
    full_prompt = f"[Cron job: {job_id}, report to Discord channel {channel}]\n\n{prompt}"

    if ephemeral:
        # Ephemeral: skip resume, go straight to new session
        result = invoke_claude(
            full_prompt,
            caller=f"cron-{job_id}",
            session_id=session,
            resume=False,
            max_turns=max_turns,
        )
    else:
        # Try --resume first, fall back to --session-id
        result = invoke_claude(
            full_prompt,
            caller=f"cron-{job_id}",
            session_id=session,
            resume=True,
            max_turns=max_turns,
        )

        # If resume failed (session not found), retry with --session-id
        if result.exit_code != 0 and "No conversation found" in result.stderr:
            logger.info("Session %s not found, creating new session", session)
            result = invoke_claude(
                full_prompt,
                caller=f"cron-{job_id}",
                session_id=session,
                resume=False,
                max_turns=max_turns,
            )

    # Update state with actual completion time
    now = _now()
    set_last_run(job_id, now)
    append_history(
        job_id,
        exit_code=result.exit_code,
        duration=result.duration,
        session_id=result.session_id,
        timestamp=now,
        cost_usd=result.cost_usd,
    )

    if result.exit_code == 0:
        logger.info("Job %s completed successfully (%.1fs)", job_id, result.duration)
        log_event("cron_dispatch", job_id=job_id, event="completed",
                  duration=round(result.duration, 3), exit_code=0)
    else:
        logger.error(
            "Job %s failed (exit=%d, %.1fs): %s",
            job_id,
            result.exit_code,
            result.duration,
            result.stderr[:200] if result.stderr else "no error message",
        )
        log_event("cron_dispatch", job_id=job_id, event="failed",
                  duration=round(result.duration, 3), exit_code=result.exit_code)


def run_single_job(job_id: str) -> None:
    """Run a specific job immediately (manual execution)."""
    logger.info("Manual execution requested for job %s", job_id)

    jobs = load_all_jobs()

    if job_id not in jobs:
        logger.error("Job %s not found", job_id)
        logger.info("Available jobs: %s", ", ".join(jobs.keys()))
        raise SystemExit(1)

    job = jobs[job_id]

    # Check if job is disabled (warn but allow manual execution)
    if not job.get("enabled", True):
        logger.warning("Job %s is disabled, but running anyway (manual execution)", job_id)

    try:
        run_job(job_id, job)
    except Exception:
        logger.exception("Unexpected error running job %s", job_id)
        raise SystemExit(1)

    logger.info("Manual execution completed")


def run_dispatcher() -> None:
    """Main dispatcher: check all jobs and run due ones in parallel."""
    now = _now()
    logger.info("Dispatcher started at %s", now.isoformat())

    jobs = load_all_jobs()
    logger.info("Loaded %d job(s)", len(jobs))

    # Collect due jobs
    due_jobs = []
    for job_id, job in jobs.items():
        # Skip disabled jobs
        if not job.get("enabled", True):
            logger.debug("Skipping disabled job %s", job_id)
            continue

        schedule = job["schedule"]
        grace = job.get("grace_minutes", DEFAULT_GRACE_MINUTES)

        # Check if due
        if not is_job_due(job_id, schedule, now, grace_minutes=grace):
            logger.debug("Job %s not due yet", job_id)
            continue

        due_jobs.append((job_id, job))

    if not due_jobs:
        logger.info("No jobs due, dispatcher finished")
        return

    logger.info("Running %d due job(s) in parallel: %s",
                len(due_jobs), ", ".join(j[0] for j in due_jobs))

    # Execute due jobs in parallel
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(run_job, job_id, job): job_id
            for job_id, job in due_jobs
        }

        for future in as_completed(futures):
            job_id = futures[future]
            try:
                future.result()
            except Exception:
                logger.exception("Unexpected error running job %s", job_id)

    logger.info("Dispatcher finished")


def _validate_config() -> None:
    """Validate required configuration at startup. Fails fast with helpful messages."""
    global CRON_TZ
    env_path = paths.bot_config_path()
    errors: list[str] = []

    if not env_path.exists():
        errors.append(
            f"Config file not found at {env_path}\n"
            f"  Copy the example and fill in your values:\n"
            f"    cp {_SCRIPT_DIR / '.env.example'} {env_path}"
        )

    tz_name = os.getenv("CRON_TIMEZONE")
    if tz_name:
        try:
            CRON_TZ = ZoneInfo(tz_name)
        except (KeyError, Exception):
            errors.append(
                f"Invalid CRON_TIMEZONE={tz_name!r}\n"
                "  Use a valid IANA timezone name, e.g.:\n"
                "    CRON_TIMEZONE=Europe/Paris\n"
                "    CRON_TIMEZONE=America/New_York\n"
                "    CRON_TIMEZONE=UTC\n"
                "  Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
            )

    if errors:
        import sys
        msg = "Configuration error(s):\n\n" + "\n\n".join(f"  {i+1}. {e}" for i, e in enumerate(errors))
        logger.error(msg)
        print(msg, file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Cron job dispatcher — runs scheduled jobs.",
        epilog="""
How it works:
  1. Reads all job files from cron-jobs/*.json
  2. For each enabled job, checks if it's due (based on schedule and last run)
  3. Skips jobs that missed their window by >15 min (staleness guard)
  4. Runs all due jobs in parallel (ThreadPoolExecutor, max 6 workers)
  5. Per-job flock prevents double dispatch from overlapping dispatchers
  6. Updates per-job state (.state/{job_id}) and history (.history.json)
  7. Logs everything to logs/cron_runner.log

Manual execution:
  # Run a specific job immediately (bypasses schedule check)
  uv run cron_runner.py --job daily-python-check

Job file format (cron-jobs/<job-id>.json):
  {
    "description": "Human-readable summary",
    "schedule": "0 9 * * *",       # Cron expression
    "prompt": "Task for Claude",   # What to do
    "channel": "123456789",        # Discord channel for results
    "enabled": true,               # Toggle on/off
    "report_mode": "silent",       # "silent" or "always"
    "max_turns": 0,                # 0 = unlimited
    "ephemeral": true,             # default; false = persistent session (costs grow)
    "grace_minutes": 15            # Optional: staleness window override
  }

Related commands:
  uv run cron_manage.py --help    # Manage jobs (add, list, enable, etc.)

Logs:
  logs/cron_runner.log            # Dispatcher log
  logs/claude/                    # Per-invocation Claude logs
  cron-jobs/.state/               # Per-job last run timestamps
  cron-jobs/.history.json         # Run history
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--job",
        metavar="JOB_ID",
        help="Run a specific job immediately (bypasses schedule check)",
    )
    args = parser.parse_args()

    _validate_config()

    if args.job:
        # Manual execution mode
        run_single_job(args.job)
    else:
        # Normal dispatcher mode
        run_dispatcher()


if __name__ == "__main__":
    main()
