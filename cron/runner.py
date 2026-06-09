"""
Cron job dispatcher — runs every minute via the scheduler in cron/__init__.py.

Reads job files from cron-jobs/, checks if each is due, and executes via Claude.
Jobs run in parallel (ThreadPoolExecutor) with per-job flock to prevent double dispatch.
"""

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# When run as subprocess (uv run runner.py), add project root and merlin-bot/ to sys.path
_PROJECT_ROOT = str(Path(__file__).parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_LIB_DIR = str(Path(_PROJECT_ROOT) / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
_BOT_DIR = str(Path(_PROJECT_ROOT) / "merlin-bot")
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

from croniter import croniter
from dotenv import load_dotenv

import paths
from lib import agent_context
from lib.engine import invoke
from cron.state import (
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

# Default max turns per job execution (0 = unlimited)
DEFAULT_MAX_TURNS = 0

# Default grace period for staleness check (minutes).
# Jobs that missed their schedule by more than this are skipped.
DEFAULT_GRACE_MINUTES = 15

# Max parallel job executions
MAX_WORKERS = 6

# Safety timeout for command jobs (seconds). A hung command would otherwise hold
# its per-job flock forever, blocking every future run of that job.
COMMAND_TIMEOUT_SECONDS = 3600

# Report mode — controls notification behavior in notify.py, not the prompt.
# The engine has no notion of silent/always — it just returns text.

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("merlin.cron")

# When running as subprocess (uv run runner.py), configure own handlers.
# When imported from main process or tests, the merlin.* hierarchy provides handlers.
if __name__ == "__main__":
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    _file_handler = logging.FileHandler(LOG_DIR / "merlin.log", encoding="utf-8")
    _file_handler.setLevel(logging.INFO)
    _file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
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

    # Validate required fields. "schedule" is always required; the action field
    # depends on the job type ("command" needs a command, otherwise a prompt).
    required = ["schedule"]
    if data.get("type") == "command":
        required.append("command")
    else:
        required.append("prompt")
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


def job_timezone(job: dict) -> ZoneInfo | None:
    """Resolve a job's scheduling timezone.

    Order: per-job ``timezone`` (if set and valid) -> server-wide ``CRON_TZ``
    (from ``CRON_TIMEZONE``) -> None (meaning system/UTC, unchanged behavior).
    """
    name = job.get("timezone")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning("Job has invalid timezone %r, using server default", name)
    return CRON_TZ


def is_job_due(
    job_id: str,
    schedule: str,
    now: datetime,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
    tz: ZoneInfo | None = None,
) -> bool:
    """Check if a job is due to run based on schedule and last run time.

    The schedule is interpreted in ``tz`` (the job's timezone) when given, else
    the server-wide ``CRON_TZ``. Interpreting in a DST-aware zone keeps a
    wall-clock schedule (e.g. "0 17 * * *" = 17:00 local) stable across DST.

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
        logger.info(
            "New job %s registered, first run at %s", job_id, next_time.isoformat()
        )
        return False

    # Normalize to the scheduling timezone so croniter interprets the schedule
    # in the right timezone (e.g. "30 7" = 7:30 local, not 7:30 UTC).
    effective_tz = tz or CRON_TZ
    if effective_tz:
        last_run = last_run.astimezone(effective_tz)
        now = now.astimezone(effective_tz)

    # Get the next scheduled time after last run
    cron = croniter(schedule, last_run)
    next_run = cron.get_next(datetime)

    if next_run > now:
        return False  # Not due yet

    # Staleness check: if the job missed its window by too much, skip it.
    # Sub-minute staleness is never "missed": the dispatcher always fires a
    # second or two after the minute (subprocess boot), so grace_minutes=0
    # would otherwise mean "never run".
    staleness_seconds = (now - next_run).total_seconds()
    grace_seconds = max(grace_minutes * 60, 59)

    if staleness_seconds > grace_seconds:
        logger.warning(
            "Job %s missed its window by %.0f min (grace=%d min), skipping — advancing state",
            job_id,
            staleness_seconds / 60,
            grace_minutes,
        )
        set_last_run(job_id, now)
        return False

    return True


def build_prompt(job: dict) -> str:
    """Build the prompt for a job. Just the job's prompt — no delivery instructions."""
    return job["prompt"]


def resolve_working_dir(job: dict) -> str:
    """Working directory for a job (both types): where the job operates.

    Resolution chain: job.working_dir -> MERLIN_LAUNCH_CWD -> $HOME
    (the env/home default lives in paths.launch_cwd, shared with the
    bot). An agent job pointed at a project repo auto-loads that repo's
    own CLAUDE.md; Merlin context arrives by injection and the skill
    adapters, not by cwd.
    """
    return job.get("working_dir") or str(paths.launch_cwd())


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """Result of a command job, mirroring the AgentResult fields that
    `_execute_job` and the notification system consume."""

    exit_code: int
    duration: float
    result: str  # combined stdout + stderr
    stderr: str = ""
    cost_usd: float | None = None
    session_id: str | None = None


def _run_command(job_id: str, job: dict) -> CommandResult:
    """Run a command job via `bash -lc`, capturing combined output and timing.

    No agent, no session, no token cost. Working directory resolved by
    the shared chain in resolve_working_dir().
    """
    command = job.get("command", "")
    cwd = resolve_working_dir(job)

    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        duration = time.monotonic() - start
        combined = (proc.stdout or "") + (proc.stderr or "")
        return CommandResult(
            exit_code=proc.returncode,
            duration=duration,
            result=combined,
            stderr=proc.stderr or "",
        )
    except subprocess.TimeoutExpired as e:
        duration = time.monotonic() - start

        def _decode(buf) -> str:
            if not buf:
                return ""
            return buf.decode(errors="replace") if isinstance(buf, bytes) else buf

        msg = f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s"
        partial = _decode(e.stdout) + _decode(e.stderr)
        logger.error("Command job %s timed out", job_id)
        return CommandResult(
            exit_code=124,
            duration=duration,
            result=(partial + "\n" + msg).strip(),
            stderr=msg,
        )
    except OSError as e:
        duration = time.monotonic() - start
        msg = f"Failed to run command (cwd={cwd!r}): {e}"
        logger.error("Command job %s could not start: %s", job_id, e)
        return CommandResult(
            exit_code=1,
            duration=duration,
            result=msg,
            stderr=msg,
        )


def _run_agent(job_id: str, job: dict, request_id: str):
    """Run a prompt job through the agent engine. Returns an AgentResult."""
    max_turns_cfg = job.get("max_turns", DEFAULT_MAX_TURNS)
    # 0 means unlimited — pass None to wrapper so --max-turns flag is omitted
    max_turns = max_turns_cfg if max_turns_cfg > 0 else None
    ephemeral = job.get("ephemeral", True)

    # Ephemeral jobs (default) get a fresh UUID each time — no session continuity.
    # Cross-run context should use the notes system (KB, logs), not session history.
    # Set "ephemeral": false to opt into persistent sessions (costs grow per run).
    if ephemeral:
        session = str(uuid.uuid4())
    else:
        session = session_id_for_job(job_id)

    prompt = build_prompt(job)

    # Build prompt — engine returns text, notification system handles delivery
    full_prompt = f"[Cron job: {job_id}]\n\n{prompt}"

    # Headless-worker recipe: brain + user memory, no personality by design
    # (operational jobs shouldn't sound like the bot).
    return invoke(
        full_prompt,
        caller=f"cron-{job_id}",
        session_id=session,
        max_turns=max_turns,
        request_id=request_id,
        cwd=Path(resolve_working_dir(job)),
        append_system_prompt=agent_context.compose("headless-worker"),
    )


def run_job(job_id: str, job: dict, *, emit_result: bool = True):
    """Execute a single cron job. Acquires per-job lock to prevent double dispatch.

    Returns the job result (AgentResult or CommandResult), or None when the
    job is already running (locked).
    """
    # Acquire per-job lock (non-blocking)
    lock = acquire_job_lock(job_id)
    if lock is None:
        logger.warning("Job %s already running (locked), skipping", job_id)
        return None

    try:
        return _execute_job(job_id, job, emit_result=emit_result)
    finally:
        release_job_lock(lock)


def _execute_job(job_id: str, job: dict, *, emit_result: bool = True):
    """Execute a job (internal — assumes lock is held). Returns the result.

    Branches on job ``type``: a ``"command"`` job runs a shell command via
    ``_run_command``; any other type (default ``"prompt"``) runs through the
    agent engine via ``_run_agent``. Both return a result exposing the same
    fields, so the state/history/log/notify tail below is shared.

    ``emit_result`` controls the structured job_complete line on stdout. It
    exists for the subprocess contract (the scheduler and the REST trigger
    parse it for notifications); in-process callers like 'merlin cron
    trigger' pass False so their own stdout stays a single JSON document.
    """
    job_type = job.get("type", "prompt")
    request_id = str(uuid.uuid4())

    logger.info("[%s] Running %s job %s", request_id[:8], job_type, job_id)
    log_event(
        "cron_dispatch",
        job_id=job_id,
        event="started",
        duration=0,
        exit_code=0,
        request_id=request_id,
    )

    # Mark as running BEFORE execution to prevent re-dispatch by concurrent schedulers
    set_last_run(job_id, _now())

    if job_type == "command":
        result = _run_command(job_id, job)
    else:
        result = _run_agent(job_id, job, request_id)

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

    # Write detailed execution log (hybrid storage)
    try:
        from cron.logs import cleanup_logs, write_log

        write_log(
            job_id,
            {
                "job_id": job_id,
                "timestamp": now.isoformat(),
                "exit_code": result.exit_code,
                "duration_seconds": round(result.duration, 2),
                "cost_usd": result.cost_usd,
                "session_id": result.session_id,
                "output": result.result,
            },
        )
        cleanup_logs(job_id)
    except Exception:
        logger.warning(
            "Failed to write execution log for job %s", job_id, exc_info=True
        )

    if result.exit_code == 0:
        logger.info(
            "[%s] Job %s completed successfully (%.1fs)",
            request_id[:8],
            job_id,
            result.duration,
        )
        log_event(
            "cron_dispatch",
            job_id=job_id,
            event="completed",
            duration=round(result.duration, 3),
            exit_code=0,
            request_id=request_id,
        )
    else:
        logger.error(
            "[%s] Job %s failed (exit=%d, %.1fs): %s",
            request_id[:8],
            job_id,
            result.exit_code,
            result.duration,
            result.stderr[:200] if result.stderr else "no error message",
        )
        log_event(
            "cron_dispatch",
            job_id=job_id,
            event="failed",
            duration=round(result.duration, 3),
            exit_code=result.exit_code,
            request_id=request_id,
        )

    # Emit structured JSON to stdout so the scheduler (main process) can
    # pick it up and send Discord notifications via notify_cron_result().
    if emit_result:
        try:
            import json as _json

            print(
                _json.dumps(
                    {
                        "type": "job_complete",
                        "job_id": job_id,
                        "job": job,
                        "result": {
                            "exit_code": result.exit_code,
                            "duration_seconds": round(result.duration, 2),
                            "cost_usd": result.cost_usd,
                            "session_id": result.session_id,
                            "output": result.result or "",
                        },
                    }
                ),
                flush=True,
            )
        except Exception:
            pass  # Never fail the job for a notification issue

    return result


def run_single_job(job_id: str, *, emit_result: bool = True):
    """Run a specific job immediately (manual execution).

    Returns the job result, or None when the job is locked (already
    running). Raises SystemExit on unknown job or unexpected errors.
    """
    logger.info("Manual execution requested for job %s", job_id)

    jobs = load_all_jobs()

    if job_id not in jobs:
        logger.error("Job %s not found", job_id)
        logger.info("Available jobs: %s", ", ".join(jobs.keys()))
        raise SystemExit(1)

    job = jobs[job_id]

    # Check if job is disabled (warn but allow manual execution)
    if not job.get("enabled", True):
        logger.warning(
            "Job %s is disabled, but running anyway (manual execution)", job_id
        )

    try:
        result = run_job(job_id, job, emit_result=emit_result)
    except Exception:
        logger.exception("Unexpected error running job %s", job_id)
        raise SystemExit(1)

    logger.info("Manual execution completed")
    return result


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

        # Check if due (interpret the schedule in the job's timezone)
        if not is_job_due(
            job_id, schedule, now, grace_minutes=grace, tz=job_timezone(job)
        ):
            logger.debug("Job %s not due yet", job_id)
            continue

        due_jobs.append((job_id, job))

    if not due_jobs:
        logger.info("No jobs due, dispatcher finished")
        return

    logger.info(
        "Running %d due job(s) in parallel: %s",
        len(due_jobs),
        ", ".join(j[0] for j in due_jobs),
    )

    # Execute due jobs in parallel
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(run_job, job_id, job): job_id for job_id, job in due_jobs
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

        msg = "Configuration error(s):\n\n" + "\n\n".join(
            f"  {i + 1}. {e}" for i, e in enumerate(errors)
        )
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
  7. Logs everything to logs/merlin.log and logs/engine-log.jsonl

Manual execution:
  # Run a specific job immediately (bypasses schedule check)
  uv run cron/runner.py --job daily-python-check

Job file format (cron-jobs/<job-id>.json):
  {
    "description": "Human-readable summary",
    "schedule": "0 9 * * *",       # Cron expression
    "timezone": "Europe/Paris",    # Optional IANA zone; default CRON_TIMEZONE then UTC
    "type": "prompt",              # "prompt" (agent, default) or "command" (shell)
    "prompt": "Task for Claude",   # Prompt jobs: what to ask the agent
    "command": "echo hi",          # Command jobs: shell command run via bash -lc
    "working_dir": null,           # Both job types: cwd; default MERLIN_LAUNCH_CWD then $HOME
    "discord_channel": "default",  # Discord destination ("default" or a channel ID)
    "enabled": true,               # Toggle on/off
    "report_mode": "silent",       # "always", "silent" (errors only), or "off"
    "max_turns": 0,                # Prompt jobs: 0 = unlimited
    "ephemeral": true,             # Prompt jobs: false = persistent session (costs grow)
    "grace_minutes": 15            # Optional: staleness window override
  }

  Command jobs run with no agent and no token cost (cost_usd is null); a
  COMMAND_TIMEOUT_SECONDS (default 3600) guard kills hung commands (exit 124).

Related commands:
  merlin cron --help              # Manage jobs (add, list, enable, etc.)

Logs:
  logs/merlin.log                 # Unified app log (shared with main process)
  logs/engine-log.jsonl           # Engine lifecycle events
  logs/raw-sessions/              # Raw engine output per invocation
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
