"""REST API endpoints and page route for job management."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from job import logs, manage, state, webhook
from job.schemas import JobCreate, JobUpdate
from lib.event_log import JobRunnerCrashEvent, InvocationEvent, read_events
from merlin_ext import make_templates
from perf.aggregate import PerformanceData, aggregate_invocations

job_router = APIRouter(prefix="/api/job", tags=["job"])
job_page_router = APIRouter(tags=["job"])

_JOB_DIR = Path(__file__).parent.resolve()

templates = make_templates(_JOB_DIR / "templates")


def _job_tz(job: dict):
    """Resolve a job's scheduling timezone: per-job `timezone` if set and valid,
    otherwise the server-wide `CRON_TIMEZONE` (UTC fallback)."""
    from zoneinfo import ZoneInfo

    from job.tz import cron_timezone

    name = job.get("timezone")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return cron_timezone()


def _enrich_job(job: dict) -> dict:
    """Add last_run and next_run to a job dict.

    next_run is computed in the job's scheduling timezone (per-job `timezone`,
    else `CRON_TIMEZONE`) so the card's "Next" matches the time the job fires.
    """
    from croniter import croniter

    tz = _job_tz(job)
    job_id = job.get("id", "")
    schedule = job.get("schedule", "")

    # The schedule cursor advances only on scheduled runs; the next-run preview
    # is measured from it. The card's "Last" display, by contrast, reflects the
    # actual last run of ANY trigger (webhook/manual included), from history.
    cursor = state.get_last_run(job_id)
    recent = state.get_history(job_id, limit=1)
    if recent:
        job["last_run"] = recent[0].get("timestamp")
    else:
        job["last_run"] = cursor.isoformat() if cursor else None

    if schedule:
        # Base croniter in the scheduling timezone so "0 9 * * *" means 09:00
        # in that zone, not 09:00 UTC.
        base = cursor.astimezone(tz) if cursor else datetime.now(tz=tz)
        try:
            cron = croniter(schedule, base)
            next_run = cron.get_next(datetime)
            # Ensure timezone-aware
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=tz)
            job["next_run"] = next_run.isoformat()
        except (KeyError, ValueError):
            job["next_run"] = None
    else:
        job["next_run"] = None

    # The URL an external sender calls, shown on the job detail/editor.
    # The source lets the editor hint when only a local address is known.
    if (job.get("webhook") or {}).get("secret"):
        base, source = webhook.resolve_public_base()
        job["webhook_url"] = f"{base}/webhooks/job/{job_id}"
        job["webhook_url_source"] = source

    return job


@job_router.get("/jobs")
def list_jobs():
    """List all jobs, enriched with last_run and next_run."""
    jobs = manage.list_jobs()
    return [_enrich_job(job) for job in jobs]


@job_router.post("/jobs", status_code=201)
def create_job(body: JobCreate):
    """Create a new job."""
    # Check uniqueness
    if manage.load_job(body.id) is not None:
        raise HTTPException(status_code=409, detail=f"Job '{body.id}' already exists")

    job = {
        "description": body.description,
        "timezone": body.timezone,
        "type": body.type,
        "prompt": body.prompt,
        "command": body.command,
        "working_dir": body.working_dir,
        "enabled": body.enabled,
        "report_mode": body.report_mode,
        "max_turns": body.max_turns,
        "ephemeral": body.ephemeral,
        "grace_minutes": body.grace_minutes,
        "discord_channel": body.discord_channel,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    # Triggers are optional: only store the keys that are actually set.
    if body.schedule:
        job["schedule"] = body.schedule
    if body.webhook is not None:
        job["webhook"] = body.webhook.model_dump()

    manage.save_job(body.id, job)

    job["id"] = body.id
    return _enrich_job(job)


@job_router.get("/jobs/{job_id}")
def get_job(job_id: str):
    """Get a single job with history."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    job["id"] = job_id
    job = _enrich_job(job)
    job["history"] = state.get_history(job_id, limit=20)
    return job


@job_router.put("/jobs/{job_id}")
def update_job(job_id: str, body: JobUpdate):
    """Update an existing job (merge non-None fields)."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    # Merge only non-None fields from the update body. An empty-string
    # schedule means "remove the schedule trigger" (None means "unchanged").
    update_data = body.model_dump(exclude_none=True)
    for key, value in update_data.items():
        if key == "schedule" and value == "":
            job.pop("schedule", None)
        else:
            job[key] = value

    # Validate the MERGED job: switching type without supplying the matching
    # action field would otherwise persist e.g. an empty command that runs
    # `bash -lc ""` and "succeeds" forever.
    if job.get("type", "prompt") == "command":
        if not (job.get("command") or "").strip():
            raise HTTPException(
                status_code=422,
                detail="command must be non-empty for a command job",
            )
    elif not (job.get("prompt") or "").strip():
        raise HTTPException(
            status_code=422,
            detail="prompt must be non-empty for a prompt job",
        )

    manage.save_job(job_id, job)

    job["id"] = job_id
    return _enrich_job(job)


@job_router.delete("/jobs/{job_id}", status_code=204)
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


@job_router.post("/jobs/{job_id}/toggle")
def toggle_job(job_id: str):
    """Toggle a job's enabled state."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    job["enabled"] = not job.get("enabled", True)
    manage.save_job(job_id, job)

    job["id"] = job_id
    return _enrich_job(job)


@job_router.post("/jobs/{job_id}/webhook")
def add_webhook(job_id: str):
    """Enable the webhook trigger: generate a secret if none exists.

    Idempotent — an existing webhook is returned unchanged (rotation is an
    explicit, separate action).
    """
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    if not (job.get("webhook") or {}).get("secret"):
        job["webhook"] = {"secret": webhook.generate_secret()}
        manage.save_job(job_id, job)

    return {"webhook": job["webhook"], "webhook_url": webhook.public_url(job_id)}


@job_router.post("/jobs/{job_id}/webhook/rotate")
def rotate_webhook(job_id: str):
    """Replace the webhook secret. The old secret stops working immediately."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if not (job.get("webhook") or {}).get("secret"):
        raise HTTPException(
            status_code=404, detail=f"Job '{job_id}' has no webhook trigger"
        )

    job["webhook"]["secret"] = webhook.generate_secret()
    manage.save_job(job_id, job)

    return {"webhook": job["webhook"], "webhook_url": webhook.public_url(job_id)}


@job_router.delete("/jobs/{job_id}/webhook", status_code=204)
def remove_webhook(job_id: str):
    """Remove the webhook trigger from a job."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    job.pop("webhook", None)
    manage.save_job(job_id, job)
    return None


@job_router.post("/jobs/{job_id}/run", status_code=202)
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
        cwd=str(_JOB_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, _stderr = await proc.communicate()

    # Process job_complete events for Discord notifications. Offloaded because
    # notification does synchronous Discord I/O that must not block the loop.
    if stdout:
        try:
            from job import _process_runner_output

            await asyncio.to_thread(_process_runner_output, stdout)
        except Exception:
            import logging

            logging.getLogger("merlin.job").warning(
                "Failed to process notifications for manual job %s",
                job_id,
                exc_info=True,
            )


@job_router.get("/jobs/{job_id}/logs")
def get_job_logs(job_id: str):
    """List execution logs for a job (newest first, no output field)."""
    job = manage.load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    return logs.list_logs(job_id)


@job_router.get("/jobs/{job_id}/logs/{timestamp:path}")
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


@job_router.get("/logs")
def get_all_logs(job_id: str | None = None, limit: int = 100):
    """List execution logs across all jobs (or filtered by job_id), newest first."""
    import paths

    all_entries: list[dict] = []
    logs_dir = paths.job_logs_dir()
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


@job_router.get("/webhook-events")
def webhook_events(job_id: str | None = None, limit: int = 50):
    """Recent webhook_request events for the job source, newest first.

    Includes rejected attempts (bad secret, throttled): failed hits on a
    public endpoint are a security signal the dashboard should surface.
    """
    from lib.event_log import WebhookRequestEvent, read_events

    limit = max(1, min(limit, 500))
    events = read_events(event_type="webhook_request")
    out: list[dict] = []
    for e in reversed(events):
        if not isinstance(e, WebhookRequestEvent) or e.source != "job":
            continue
        if job_id and e.target != job_id:
            continue
        out.append(e.model_dump(exclude_unset=True))
        if len(out) >= limit:
            break
    return out


@job_router.post("/validate-schedule")
def validate_schedule(request_body: dict):
    """Validate a cron expression and return next 3 run times.

    Runs are computed and preformatted in the requested timezone (the modal
    sends the job's selected timezone; falls back to CRON_TIMEZONE, then UTC),
    so the preview reflects when the job actually fires.
    """
    from zoneinfo import ZoneInfo

    from croniter import croniter

    from job.tz import cron_timezone

    schedule = request_body.get("schedule", "")
    tz_name = request_body.get("timezone")
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = cron_timezone()
    else:
        tz = cron_timezone()
    try:
        c = croniter(schedule, datetime.now(tz=tz))
        next_runs = []
        for _ in range(3):
            run = c.get_next(datetime)
            if run.tzinfo is None:
                run = run.replace(tzinfo=tz)
            # Preformat in the cron timezone, e.g. "Wed Jun 4, 09:00".
            next_runs.append(
                run.strftime("%a %b ") + str(run.day) + run.strftime(", %H:%M")
            )
        human = manage.cron_to_human(schedule)
        return {
            "valid": True,
            "human": human,
            "timezone": str(tz),
            "next_runs": next_runs,
        }
    except (KeyError, ValueError):
        return {"valid": False, "error": "Invalid cron expression"}


def _get_recent_crashes() -> list[JobRunnerCrashEvent]:
    """Recent job_runner_crash events (last 24h), via the shared event reader.

    The shared reader already handles a missing/empty file and malformed lines.
    We only guard against a read race (file removed mid-read) so the jobs page
    never 500s on a transient I/O error.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    try:
        events = read_events(event_type="job_runner_crash", since=cutoff)
    except OSError:
        return []
    return [e for e in events if isinstance(e, JobRunnerCrashEvent)]


@job_router.get("/crashes")
def get_crashes():
    """Get recent job_runner_crash events (last 24h)."""
    return [c.model_dump(exclude_unset=True) for c in _get_recent_crashes()]


@job_router.get("/performance")
def job_performance(since: str | None = None) -> PerformanceData:
    """Aggregate performance metrics for job callers (caller starts with 'job-').

    The job analogue of the bot performance view: read invocation events from
    engine-log.jsonl, keep only job callers, and aggregate server-side. A
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
    job_events = [
        e
        for e in raw
        if isinstance(e, InvocationEvent) and (e.caller or "").startswith("job-")
    ]
    return aggregate_invocations(job_events, now)


# ---------------------------------------------------------------------------
# Dashboard page route
# ---------------------------------------------------------------------------


@job_page_router.get("/jobs", response_class=HTMLResponse, include_in_schema=False)
def jobs_page(request: Request):
    """Render the jobs dashboard page."""
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

    # The Webhooks tab (activity + rejected attempts) only appears when at
    # least one job is webhook-firable, so it doesn't clutter non-webhook use.
    any_webhook = any((job.get("webhook") or {}).get("secret") for job in jobs)

    # Resolve the default working directory for jobs (modal placeholder).
    import paths

    default_working_dir = str(paths.launch_cwd())

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "bot_loaded": bot_loaded,
            "any_webhook": any_webhook,
            "crashes": crashes,
            "default_working_dir": default_working_dir,
        },
    )
