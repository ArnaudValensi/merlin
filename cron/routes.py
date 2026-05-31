"""REST API endpoints and page route for cron job management."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from cron import logs, manage, state
from cron.schemas import JobCreate, JobUpdate
from lib.event_log import CronRunnerCrashEvent, InvocationEvent, read_events
from merlin_ext import make_templates
from perf.aggregate import PerformanceData, aggregate_invocations

cron_router = APIRouter(prefix="/api/cron", tags=["cron"])
cron_page_router = APIRouter(tags=["cron"])

_CRON_DIR = Path(__file__).parent.resolve()

templates = make_templates(_CRON_DIR / "templates")


def _enrich_job(job: dict) -> dict:
    """Add last_run and next_run to a job dict."""
    from croniter import croniter

    job_id = job.get("id", "")
    schedule = job.get("schedule", "")

    last_run = state.get_last_run(job_id)
    job["last_run"] = last_run.isoformat() if last_run else None

    if schedule:
        base = last_run or datetime.now(tz=timezone.utc)
        try:
            cron = croniter(schedule, base)
            next_run = cron.get_next(datetime)
            # Ensure timezone-aware
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)
            job["next_run"] = next_run.isoformat()
        except (KeyError, ValueError):
            job["next_run"] = None
    else:
        job["next_run"] = None

    return job


@cron_router.get("/jobs")
def list_jobs():
    """List all cron jobs, enriched with last_run and next_run."""
    jobs = manage.list_jobs()
    return [_enrich_job(job) for job in jobs]


@cron_router.post("/jobs", status_code=201)
def create_job(body: JobCreate):
    """Create a new cron job."""
    # Check uniqueness
    if manage.load_job(body.id) is not None:
        raise HTTPException(status_code=409, detail=f"Job '{body.id}' already exists")

    job = {
        "description": body.description,
        "schedule": body.schedule,
        "prompt": body.prompt,
        "enabled": body.enabled,
        "report_mode": body.report_mode,
        "max_turns": body.max_turns,
        "ephemeral": body.ephemeral,
        "grace_minutes": body.grace_minutes,
        "discord_channel": body.discord_channel,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    manage.save_job(body.id, job)

    job["id"] = body.id
    return _enrich_job(job)


@cron_router.get("/jobs/{job_id}")
def get_job(job_id: str):
    """Get a single job with history."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    job["id"] = job_id
    job = _enrich_job(job)
    job["history"] = state.get_history(job_id, limit=20)
    return job


@cron_router.put("/jobs/{job_id}")
def update_job(job_id: str, body: JobUpdate):
    """Update an existing job (merge non-None fields)."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    # Merge only non-None fields from the update body
    update_data = body.model_dump(exclude_none=True)
    for key, value in update_data.items():
        job[key] = value

    manage.save_job(job_id, job)

    job["id"] = job_id
    return _enrich_job(job)


@cron_router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str):
    """Delete a job and clean up state/locks."""
    if not manage.delete_job(job_id):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    # Clean up state file
    state_file = state.STATE_DIR / job_id
    if state_file.exists():
        state_file.unlink()

    # Clean up lock file
    lock_file = state.LOCKS_DIR / f"{job_id}.lock"
    if lock_file.exists():
        lock_file.unlink()

    # Clean up execution logs
    logs.delete_logs(job_id)

    return None


@cron_router.post("/jobs/{job_id}/toggle")
def toggle_job(job_id: str):
    """Toggle a job's enabled state."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    job["enabled"] = not job.get("enabled", True)
    manage.save_job(job_id, job)

    job["id"] = job_id
    return _enrich_job(job)


@cron_router.post("/jobs/{job_id}/run", status_code=202)
async def run_job(job_id: str):
    """Trigger an immediate run of a job."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    # Run in background — capture stdout for notification processing
    asyncio.create_task(_run_job_with_notify(job_id))

    return {"ok": True, "message": f"Job '{job_id}' triggered"}


async def _run_job_with_notify(job_id: str) -> None:
    """Run a job subprocess and process notifications from its output."""
    proc = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "runner.py",
        "--job",
        job_id,
        cwd=str(_CRON_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, _stderr = await proc.communicate()

    # Process job_complete events for Discord notifications
    if stdout:
        try:
            from cron import _process_runner_output

            _process_runner_output(stdout)
        except Exception:
            import logging

            logging.getLogger("merlin.cron").warning(
                "Failed to process notifications for manual job %s",
                job_id,
                exc_info=True,
            )


@cron_router.get("/jobs/{job_id}/logs")
def get_job_logs(job_id: str):
    """List execution logs for a job (newest first, no output field)."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    return logs.list_logs(job_id)


@cron_router.get("/jobs/{job_id}/logs/{timestamp:path}")
def get_job_log(job_id: str, timestamp: str):
    """Read a specific execution log by timestamp."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    log_entry = logs.read_log(job_id, timestamp)
    if log_entry is None:
        raise HTTPException(status_code=404, detail="Log not found")

    return log_entry


# ---------------------------------------------------------------------------
# Schedule validation endpoint
# ---------------------------------------------------------------------------


@cron_router.get("/logs")
def get_all_logs(job_id: str | None = None, limit: int = 100):
    """List execution logs across all jobs (or filtered by job_id), newest first."""
    import paths

    all_entries: list[dict] = []
    logs_dir = paths.cron_logs_dir()
    if not logs_dir.exists():
        return []

    if job_id:
        # Single job
        entries = logs.list_logs(job_id, limit=limit)
        for e in entries:
            e["job_id"] = job_id
        all_entries = entries
    else:
        # All jobs
        for job_dir in sorted(logs_dir.iterdir()):
            if job_dir.is_dir():
                entries = logs.list_logs(job_dir.name, limit=limit)
                for e in entries:
                    e.setdefault("job_id", job_dir.name)
                all_entries.extend(entries)
        # Sort newest first
        all_entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        all_entries = all_entries[:limit]

    # Resolve session_id → session_file for session viewer links
    session_dir = paths.logs_dir() / "raw-sessions"
    if session_dir.exists():
        for entry in all_entries:
            sid = entry.get("session_id")
            if sid:
                matches = list(session_dir.glob(f"*-{sid}.jsonl"))
                if matches:
                    entry["session_file"] = matches[0].name

    return all_entries


@cron_router.post("/validate-schedule")
def validate_schedule(request_body: dict):
    """Validate a cron expression and return next 3 run times."""
    from croniter import croniter

    schedule = request_body.get("schedule", "")
    try:
        c = croniter(schedule, datetime.now(tz=timezone.utc))
        next_runs = [c.get_next(datetime).isoformat() for _ in range(3)]
        human = manage.cron_to_human(schedule)
        return {"valid": True, "next_runs": next_runs, "human": human}
    except (KeyError, ValueError):
        return {"valid": False, "error": "Invalid cron expression"}


def _get_recent_crashes() -> list[CronRunnerCrashEvent]:
    """Recent cron_runner_crash events (last 24h), via the shared event reader.

    The shared reader already handles a missing/empty file and malformed lines.
    We only guard against a read race (file removed mid-read) so the cron page
    never 500s on a transient I/O error.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    try:
        events = read_events(event_type="cron_runner_crash", since=cutoff)
    except OSError:
        return []
    return [e for e in events if isinstance(e, CronRunnerCrashEvent)]


@cron_router.get("/crashes")
def get_crashes():
    """Get recent cron_runner_crash events (last 24h)."""
    return [c.model_dump(exclude_unset=True) for c in _get_recent_crashes()]


@cron_router.get("/performance")
def cron_performance(since: str | None = None) -> PerformanceData:
    """Aggregate performance metrics for cron callers (caller starts with 'cron-').

    The cron analogue of the bot performance view: read invocation events from
    engine-log.jsonl, keep only cron callers, and aggregate server-side. A
    missing/empty log yields a zeroed PerformanceData (HTTP 200, not 404).
    """
    now = datetime.now(tz=timezone.utc)
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid 'since' timestamp"
            ) from None
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
    else:
        since_dt = now - timedelta(days=7)

    raw = read_events(event_type="invocation", since=since_dt)
    cron_events = [
        e
        for e in raw
        if isinstance(e, InvocationEvent) and (e.caller or "").startswith("cron-")
    ]
    return aggregate_invocations(cron_events, now)


# ---------------------------------------------------------------------------
# Dashboard page route
# ---------------------------------------------------------------------------


@cron_page_router.get("/cron", response_class=HTMLResponse, include_in_schema=False)
def cron_page(request: Request):
    """Render the cron jobs dashboard page."""
    jobs = manage.list_jobs()

    for job in jobs:
        _enrich_job(job)

        # Last run status from history
        job_id = job.get("id", "")
        history = state.get_history(job_id, limit=1)
        if history:
            job["last_status"] = history[0].get("exit_code", -1)
            job["last_cost"] = history[0].get("cost_usd")
            job["last_duration"] = history[0].get("duration")
        else:
            job["last_status"] = None
            job["last_cost"] = None
            job["last_duration"] = None

        # Human-readable schedule
        job["schedule_human"] = manage.cron_to_human(job.get("schedule", ""))

    # Check if merlin-bot is loaded (for Discord notification UI)
    try:
        from main import extension_registry

        bot_info = extension_registry.get("merlin-bot")
        bot_loaded = bool(bot_info and bot_info.loaded)
    except (ImportError, AttributeError):
        bot_loaded = False

    # Check for recent scheduler crashes
    crashes = _get_recent_crashes()

    return templates.TemplateResponse(
        request,
        "cron.html",
        {
            "jobs": jobs,
            "bot_loaded": bot_loaded,
            "crashes": crashes,
        },
    )
