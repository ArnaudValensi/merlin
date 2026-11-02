"""
Job dispatcher: runs every minute via the scheduler in job/__init__.py.

Reads job files from jobs/, checks if each is due, and runs the due ones in
parallel (ThreadPoolExecutor) with a per-job flock against double dispatch.
Every run, agent or script, opens a window in the ``merlin-jobs`` tmux
session (job/runs.py) and is watched to its end by the supervisor
(job/supervisor.py) in the thread that holds the flock. Each tick also
finalizes orphaned runs and closes windows past their retention
(job/retention.py).
"""

import json
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# When run as subprocess (uv run runner.py), add project root and lib/ to sys.path
_PROJECT_ROOT = str(Path(__file__).parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_LIB_DIR = str(Path(_PROJECT_ROOT) / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from croniter import croniter
from dotenv import load_dotenv

import paths
from job import retention, runs, supervisor
from job.runs import LaunchError, Run
from job.state import (
    acquire_job_lock,
    append_history,
    get_last_run,
    release_job_lock,
    set_last_run,
)
from job.supervisor import Outcome
from structured_log import log_event

_SCRIPT_DIR = Path(__file__).parent.resolve()

load_dotenv(paths.bot_config_path())

# Timezone for interpreting job schedules. Loaded lazily in _validate_config().
JOB_TZ: ZoneInfo | None = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JOBS_DIR = paths.jobs_dir()
LOG_DIR = paths.logs_dir()

# Default grace period for staleness check (minutes).
# Jobs that missed their schedule by more than this are skipped.
DEFAULT_GRACE_MINUTES = 15

# Max parallel job executions
MAX_WORKERS = 6

# Deadlines (seconds). A run past its deadline is ended (exit 124) so a hung
# job never holds its per-job flock forever: one hour for a script, six for
# an agent.
COMMAND_TIMEOUT_SECONDS = 3600
AGENT_TIMEOUT_SECONDS = 21600

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("merlin.job")

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


def load_job(path: Path) -> dict | None:
    """Load and validate a job file. Returns None if invalid."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load job %s: %s", path.name, e)
        return None

    # Validate required fields. The action field depends on the job type
    # ("command" needs a command, otherwise a prompt). "schedule" is optional:
    # a job without one has no schedule trigger (webhook-only or manual-only).
    required = ["command"] if data.get("type") == "command" else ["prompt"]
    missing = [f for f in required if f not in data]
    if missing:
        logger.warning("Job %s missing required fields: %s", path.name, missing)
        return None

    # Validate cron expression (only when a schedule is present)
    if data.get("schedule"):
        try:
            croniter(data["schedule"])
        except (KeyError, ValueError) as e:
            logger.warning("Job %s has invalid schedule: %s", path.name, e)
            return None

    return data


def load_all_jobs() -> dict[str, dict]:
    """Load all valid job files. Returns {job_id: job_data}."""
    jobs = {}
    if not JOBS_DIR.exists():
        return jobs

    for path in JOBS_DIR.glob("*.json"):
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
    if JOB_TZ:
        return datetime.now(tz=JOB_TZ)
    return datetime.now(tz=timezone.utc)


def job_timezone(job: dict) -> ZoneInfo | None:
    """Resolve a job's scheduling timezone.

    Order: per-job ``timezone`` (if set and valid) -> server-wide ``JOB_TZ``
    (from ``JOB_TIMEZONE``) -> None (meaning system/UTC, unchanged behavior).
    """
    name = job.get("timezone")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning("Job has invalid timezone %r, using server default", name)
    return JOB_TZ


def is_job_due(
    job_id: str,
    schedule: str,
    now: datetime,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
    tz: ZoneInfo | None = None,
) -> bool:
    """Check if a job is due to run based on schedule and last run time.

    The schedule is interpreted in ``tz`` (the job's timezone) when given, else
    the server-wide ``JOB_TZ``. Interpreting in a DST-aware zone keeps a
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
    effective_tz = tz or JOB_TZ
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


def resolve_working_dir(job: dict) -> str:
    """Working directory for a job (both types): where the job operates.

    Resolution chain: job.working_dir -> MERLIN_LAUNCH_CWD -> $HOME (the
    env/home default lives in paths.launch_cwd). An agent job pointed at a
    project repo picks up that repo's own CLAUDE.md, exactly like an agent
    opened there by hand.
    """
    return job.get("working_dir") or str(paths.launch_cwd())


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """What a run ended with, for the caller (``merlin job trigger``, the
    scheduler's stdout line)."""

    exit_code: int
    duration: float
    result: str  # the run's output (a command's log, an agent's pane)
    stderr: str = ""
    run_id: str = ""
    agent: str | None = None
    window: str = ""  # session:window_id while the window exists


def deadline_for(job: dict) -> float:
    return (
        COMMAND_TIMEOUT_SECONDS
        if job.get("type", "prompt") == "command"
        else AGENT_TIMEOUT_SECONDS
    )


def run_job(
    job_id: str, job: dict, *, emit_result: bool = True, trigger: str = "manual"
):
    """Execute a single job. Acquires per-job lock to prevent double dispatch.

    Returns the ``RunResult``, or None when the job is already running
    (locked).
    """
    # Acquire per-job lock (non-blocking)
    lock = acquire_job_lock(job_id)
    if lock is None:
        logger.warning("Job %s already running (locked), skipping", job_id)
        return None

    try:
        return _execute_job(job_id, job, emit_result=emit_result, trigger=trigger)
    finally:
        release_job_lock(lock)


def _execute_job(
    job_id: str,
    job: dict,
    *,
    emit_result: bool = True,
    trigger: str = "manual",
    request_id: str | None = None,
) -> RunResult:
    """Execute a job (internal: assumes the lock is held). Returns the result.

    Opens the run's window (``runs.new_run`` then ``runs.launch``), waits for
    it with the supervisor, then records history and the run log. A launch
    that cannot happen (no tmux, the agent binary missing, its hooks not
    installed) is recorded at once as a failed run with no window.

    ``emit_result`` controls the structured job_complete line on stdout. It
    exists for the subprocess contract (the scheduler and the REST trigger
    parse it); in-process callers like 'merlin job trigger' pass False so
    their own stdout stays a single JSON document.

    ``trigger`` records what launched the run (schedule / webhook / manual)
    in events, history, and the run log. Webhook fires pass their
    ``request_id`` in so the run correlates with the webhook_request event
    that accepted it; it doubles as the run id.
    """
    job_type = job.get("type", "prompt")
    if request_id is None:
        request_id = str(uuid.uuid4())

    logger.info("[%s] Running %s job %s", request_id[:8], job_type, job_id)
    log_event(
        "job_dispatch",
        job_id=job_id,
        event="started",
        duration=0,
        exit_code=0,
        request_id=request_id,
        trigger=trigger,
    )

    # Advance the schedule cursor only for scheduled runs. The cursor is what
    # is_job_due measures the next fire from, so a webhook or manual run must
    # NOT move it: a fire that overlaps a scheduled slot would otherwise push
    # the cursor past that slot and silently swallow it. The per-job flock is
    # the real double-dispatch guard; every trigger still records history and
    # logs below, which drive the "Last" display.
    is_scheduled = trigger == "schedule"

    # Mark as running BEFORE execution so a concurrent scheduler tick sees the
    # slot claimed.
    if is_scheduled:
        set_last_run(job_id, _now())

    started = _now()
    run: Run | None = None
    try:
        run = runs.new_run(
            job_id,
            job,
            trigger=trigger,
            run_id=request_id,
            cwd=resolve_working_dir(job),
            deadline=deadline_for(job),
            now=started.timestamp(),
        )
        runs.launch(run)
    except LaunchError as e:
        logger.error(
            "[%s] Job %s could not start: %s", request_id[:8], job_id, e.message
        )
        if run is not None:
            runs.remove(run.dir)
        result = _record_failure(
            job_id, job, request_id, e, trigger=trigger, started=started
        )
    else:
        outcome = supervisor.supervise(run)
        result = record_run(run, outcome)

    # Advance the cursor to completion time (scheduled runs only).
    if is_scheduled:
        set_last_run(job_id, _now())

    # Emit structured JSON to stdout so the scheduler (main process) can
    # pick it up for the job_complete contract.
    if emit_result:
        try:
            print(
                json.dumps(
                    {
                        "type": "job_complete",
                        "job_id": job_id,
                        "job": job,
                        "result": {
                            "exit_code": result.exit_code,
                            "duration_seconds": round(result.duration, 2),
                            "output": result.result or "",
                        },
                    }
                ),
                flush=True,
            )
        except Exception:
            pass  # Never fail the job for a notification issue

    return result


def _record_failure(
    job_id: str,
    job: dict,
    request_id: str,
    error: LaunchError,
    *,
    trigger: str,
    started: datetime,
) -> RunResult:
    """Record a launch that could not happen: a failed run with no window."""
    agent = None
    if job.get("type", "prompt") != "command":
        from job import agents

        agent = agents.resolve(job).id
    now = _now()
    _write_record(
        job_id,
        exit_code=error.exit_code,
        duration=(now - started).total_seconds(),
        output=error.message,
        trigger=trigger,
        request_id=request_id,
        agent=agent,
        timestamp=now,
        stderr=error.message,
    )
    return RunResult(
        exit_code=error.exit_code,
        duration=(now - started).total_seconds(),
        result=error.message,
        stderr=error.message,
        run_id=request_id,
        agent=agent,
    )


def record_run(run: Run, outcome: Outcome) -> RunResult:
    """Record a finished run (history, run log, events) exactly once: the
    run directory is marked recorded, and a second call for the same run
    (the orphan path racing a supervisor) writes nothing."""
    duration = max(0.0, outcome.finished - run.started)
    result = RunResult(
        exit_code=outcome.exit_code,
        duration=duration,
        result=outcome.output,
        stderr=outcome.note,
        run_id=run.run_id,
        agent=run.agent,
        window=run.target,
    )
    if runs.is_recorded(run.dir):
        return result
    timestamp = datetime.fromtimestamp(outcome.finished, tz=timezone.utc)
    if JOB_TZ:
        timestamp = timestamp.astimezone(JOB_TZ)
    _write_record(
        run.job_id,
        exit_code=outcome.exit_code,
        duration=duration,
        output=outcome.output,
        trigger=run.trigger,
        request_id=run.run_id,
        agent=run.agent,
        timestamp=timestamp,
        stderr=outcome.note,
    )
    try:
        runs.mark_recorded(
            run.dir,
            {"exit_code": outcome.exit_code, "finished": outcome.finished},
        )
    except OSError:
        logger.warning("Could not mark run %s recorded", run.run_id, exc_info=True)
    return result


def _write_record(
    job_id: str,
    *,
    exit_code: int,
    duration: float,
    output: str,
    trigger: str,
    request_id: str,
    agent: str | None,
    timestamp: datetime,
    stderr: str = "",
) -> None:
    append_history(
        job_id,
        exit_code=exit_code,
        duration=duration,
        timestamp=timestamp,
        trigger=trigger,
        agent=agent,
        run_id=request_id,
    )

    # Write detailed execution log (hybrid storage)
    try:
        from job.logs import cleanup_logs, write_log

        write_log(
            job_id,
            {
                "job_id": job_id,
                "timestamp": timestamp.isoformat(),
                "exit_code": exit_code,
                "duration_seconds": round(duration, 2),
                "trigger": trigger,
                "agent": agent,
                "run_id": request_id,
                "output": output,
            },
        )
        cleanup_logs(job_id)
    except Exception:
        logger.warning(
            "Failed to write execution log for job %s", job_id, exc_info=True
        )

    if exit_code == 0:
        logger.info(
            "[%s] Job %s completed successfully (%.1fs)",
            request_id[:8],
            job_id,
            duration,
        )
        log_event(
            "job_dispatch",
            job_id=job_id,
            event="completed",
            duration=round(duration, 3),
            exit_code=0,
            request_id=request_id,
            trigger=trigger,
        )
    else:
        logger.error(
            "[%s] Job %s failed (exit=%d, %.1fs): %s",
            request_id[:8],
            job_id,
            exit_code,
            duration,
            stderr[:200] if stderr else "no error message",
        )
        log_event(
            "job_dispatch",
            job_id=job_id,
            event="failed",
            duration=round(duration, 3),
            exit_code=exit_code,
            request_id=request_id,
            trigger=trigger,
        )


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
    """Main dispatcher: finalize orphans, close windows past their
    retention, then check all jobs and run the due ones in parallel."""
    now = _now()
    logger.info("Dispatcher started at %s", now.isoformat())

    counts = retention.tick()
    if any(counts.values()):
        logger.info(
            "Retention: %d orphan(s) finalized, %d window(s) closed, %d run dir(s) removed",
            counts["finalized"],
            counts["closed"],
            counts["removed"],
        )

    jobs = load_all_jobs()
    logger.info("Loaded %d job(s)", len(jobs))

    # Collect due jobs
    due_jobs = []
    for job_id, job in jobs.items():
        # Skip disabled jobs
        if not job.get("enabled", True):
            logger.debug("Skipping disabled job %s", job_id)
            continue

        # Skip jobs without a schedule trigger (webhook-only or manual-only)
        schedule = job.get("schedule")
        if not schedule:
            logger.debug("Job %s has no schedule, skipping", job_id)
            continue

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
            executor.submit(run_job, job_id, job, trigger="schedule"): job_id
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
    global JOB_TZ
    env_path = paths.bot_config_path()
    errors: list[str] = []

    if not env_path.exists():
        errors.append(
            f"Config file not found at {env_path}\n"
            f"  Copy the example and fill in your values:\n"
            f"    cp {_SCRIPT_DIR / '.env.example'} {env_path}"
        )

    # CRON_TIMEZONE is the deprecated alias, still honored for back-compat.
    tz_name = os.getenv("JOB_TIMEZONE") or os.getenv("CRON_TIMEZONE")
    if tz_name:
        try:
            JOB_TZ = ZoneInfo(tz_name)
        except (KeyError, Exception):
            errors.append(
                f"Invalid JOB_TIMEZONE={tz_name!r}\n"
                "  Use a valid IANA timezone name, e.g.:\n"
                "    JOB_TIMEZONE=Europe/Paris\n"
                "    JOB_TIMEZONE=America/New_York\n"
                "    JOB_TIMEZONE=UTC\n"
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
        description="Cron job dispatcher: runs scheduled jobs.",
        epilog="""
How it works:
  1. Reads all job files from jobs/*.json
  2. For each enabled job, checks if it's due (based on schedule and last run)
  3. Skips jobs that missed their window by >15 min (staleness guard)
  4. Runs all due jobs in parallel (ThreadPoolExecutor, max 6 workers),
     each in its own window of the merlin-jobs tmux session, and waits
     for each run to finish (its exit file, the agent's state, a deadline)
  5. Per-job flock prevents double dispatch from overlapping dispatchers
  6. Updates per-job state (.state/{job_id}) and history (.history.json)
  7. Finalizes runs whose supervisor died and closes finished windows past
     their retention
  8. Logs everything to logs/merlin.log and logs/engine-log.jsonl

Manual execution:
  # Run a specific job immediately (bypasses schedule check)
  uv run job/runner.py --job daily-python-check

Job file format (jobs/<job-id>.json):
  {
    "description": "Human-readable summary",
    "schedule": "0 9 * * *",       # Cron expression
    "timezone": "Europe/Paris",    # Optional IANA zone; default JOB_TIMEZONE then UTC
    "type": "prompt",              # "prompt" (agent, default) or "command" (shell)
    "agent": "claude",             # Prompt jobs: "claude" (default) or "codex"
    "prompt": "Task for the agent",  # Prompt jobs: what to ask the agent
    "command": "echo hi",          # Command jobs: shell command run via bash -lc
    "working_dir": null,           # Both job types: cwd; default MERLIN_LAUNCH_CWD then $HOME
    "discord_channel": "default",  # Discord destination ("default" or a channel ID)
    "enabled": true,               # Toggle on/off
    "report_mode": "silent",       # "always", "silent" (errors only), or "off"
    "grace_minutes": 15            # Optional: staleness window override
  }

  Every run opens a window in the merlin-jobs tmux session. Agent jobs run
  the agent's real interface with every permission check bypassed. A run
  past its deadline (COMMAND_TIMEOUT_SECONDS 3600, AGENT_TIMEOUT_SECONDS
  21600) is ended with exit 124.

Related commands:
  merlin job --help              # Manage jobs (add, list, enable, etc.)

Logs:
  logs/merlin.log                 # Unified app log (shared with main process)
  logs/engine-log.jsonl           # Lifecycle events
  job-runs/<run_id>/              # A run's script, prompt, exit code, output
  jobs/.state/               # Per-job last run timestamps
  jobs/.history.json         # Run history
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
