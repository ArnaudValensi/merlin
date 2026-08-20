"""Webhook trigger for jobs — the ``job`` source behind the webhooks front desk.

The front desk (``webhooks/``) owns the shared, security-sensitive plumbing:
secret verification, the failed-secret throttle, and request logging. This
module owns everything job-specific:

  - resolving a job id to a :class:`webhooks.WebhookTarget` (``resolve``,
    registered for the ``job`` source in ``main.py``)
  - the secret lifecycle (``generate_secret``)
  - single-flight launching: one active run per job; extra fires coalesce
  - the public URL a sender should call (``public_url``)

Runs launch in-process (``asyncio.to_thread`` around the runner) instead of
the scheduler's subprocess path so the single-flight answer is authoritative:
the in-flight map tracks webhook runs in this process, and the per-job flock
covers overlap with scheduled/manual runs. Each launched run gets a fresh
session (independent incidents must not bleed together) — enforced in
``runner._run_agent`` for ``trigger="webhook"``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import time
import uuid
from urllib.parse import urlparse

from job import manage, state
from webhooks import FireResult, WebhookTarget

logger = logging.getLogger("merlin.job")

SECRET_PREFIX = "whk_"

# Webhook-launched runs currently in flight in this process: job_id -> run_id.
_in_flight: dict[str, str] = {}

# Keep strong references to launch tasks (asyncio only holds weak ones).
_tasks: set[asyncio.Task] = set()


def generate_secret() -> str:
    """Generate a fresh webhook secret (urlsafe, prefixed for greppability)."""
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def resolve(job_id: str) -> WebhookTarget | None:
    """Resolve a job id for the front desk. None = not a webhook target.

    A job without a ``webhook`` block is not webhook-firable — same 404 as a
    missing job, so an outside caller cannot map which jobs exist.
    """
    job = manage.load_job(job_id)
    if job is None:
        return None
    hook = job.get("webhook") or {}
    hook_secret = hook.get("secret")
    if not hook_secret:
        return None
    return WebhookTarget(
        secret=hook_secret,
        enabled=job.get("enabled", True),
        fire=lambda: _fire(job_id, job),
    )


def _fire(job_id: str, job: dict) -> FireResult:
    """Launch the job unless a run is already in flight (single-flight).

    Called by the front desk after the secret check, inside the event loop.
    Coalescing: a second fire while a run is active returns 200/"coalesced"
    and starts nothing — BetterStack firing five times during one incident
    spawns exactly one run.
    """
    existing = _in_flight.get(job_id)
    if existing is not None:
        logger.info("Webhook fire for %s coalesced (run %s active)", job_id, existing)
        return FireResult(status="coalesced", run_id=existing)

    # A scheduled or manual run holds the per-job flock; coalesce into it.
    lock = state.acquire_job_lock(job_id)
    if lock is None:
        logger.info("Webhook fire for %s coalesced (job locked)", job_id)
        return FireResult(status="coalesced", run_id=None)

    run_id = str(uuid.uuid4())
    _in_flight[job_id] = run_id
    task = asyncio.create_task(_run(job_id, job, run_id, lock))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return FireResult(status="launched", run_id=run_id)


async def _run(job_id: str, job: dict, run_id: str, lock) -> None:
    """Execute the run in a worker thread, then notify.

    Both the run and the notification are offloaded with ``asyncio.to_thread``
    so nothing blocks the event loop (the run invokes the agent; notification
    does synchronous Discord I/O). The flock is released as soon as the run
    finishes — before notifying — so single-flight covers only the run itself,
    not the delivery that follows it.
    """
    from job import runner

    result = None
    try:
        result = await asyncio.to_thread(
            runner._execute_job,
            job_id,
            job,
            emit_result=False,
            trigger="webhook",
            request_id=run_id,
        )
    except Exception:
        logger.exception("Webhook-triggered run of job %s crashed", job_id)
    finally:
        state.release_job_lock(lock)
        _in_flight.pop(job_id, None)

    if result is not None:
        try:
            await asyncio.to_thread(_notify, job_id, job, result)
        except Exception:
            logger.warning(
                "Failed to notify for webhook run of %s", job_id, exc_info=True
            )


def _notify(job_id: str, job: dict, result) -> None:
    """Deliver the run result through the shared notification path."""
    try:
        from main import extension_registry

        from job.notify import notify_job_result

        notify_job_result(
            job_id=job_id,
            job=job,
            result={
                "exit_code": result.exit_code,
                "duration_seconds": round(result.duration, 2),
                "cost_usd": result.cost_usd,
                "session_id": result.session_id,
                "output": result.result or "",
            },
            extension_registry=extension_registry,
        )
    except Exception:
        logger.warning("Failed to notify for webhook run of %s", job_id, exc_info=True)


# ---------------------------------------------------------------------------
# Public URL derivation
# ---------------------------------------------------------------------------

# The public host in SaaS mode belongs to the portal and can change under a
# running instance (environment rename), so it is resolved at read time via
# the portal's whoami endpoint — never persisted. A short in-process memo
# keeps repeated reads (e.g. enriching a job list) to one HTTP call, and the
# last good answer survives portal hiccups for the life of the process.
_WHOAMI_TTL_SECONDS = 300
_whoami_host: str | None = None
_whoami_at: float | None = None


def _saas_public_host() -> str | None:
    """The instance's public host per the portal, or None outside SaaS mode.

    Never raises: on any failure it returns the last known host (stale is
    better than wrong-with-confidence) or None so callers fall through.
    """
    global _whoami_host, _whoami_at

    token = os.getenv("MERLIN_SAAS_TOKEN", "").strip()
    if not token:
        return None

    now = time.monotonic()
    if _whoami_at is not None and now - _whoami_at < _WHOAMI_TTL_SECONDS:
        return _whoami_host

    import json
    import urllib.error
    import urllib.request

    api = os.getenv("MERLIN_SAAS_API", "https://merlincloud.dev").rstrip("/")
    req = urllib.request.Request(
        f"{api}/api/instance/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            host = json.loads(resp.read().decode()).get("public_host")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        logger.debug("whoami lookup failed, keeping last known host: %s", e)
        # Memoize the failure too, so a down portal costs one 3s timeout
        # per TTL window, not one per read.
        _whoami_at = now
        return _whoami_host

    _whoami_host = host or None
    _whoami_at = now
    return _whoami_host


def resolve_public_base() -> tuple[str, str]:
    """The instance's public base URL and how it was derived.

    Returns ``(base_url, source)`` where source is one of:
      - ``override`` — ``MERLIN_DASHBOARD_URL``, the explicit operator setting
        (own tunnel or reverse proxy; the one case that cannot be discovered).
      - ``saas`` — the portal's whoami answer (SaaS mode, BYOI and managed
        alike), resolved at read time so a slug rename is picked up
        automatically.
      - ``slug`` — ``MERLIN_ENVIRONMENT_SLUG``, the managed-container fallback
        when the portal is unreachable before whoami ever succeeded.
      - ``ip`` — detected local IP + server port. May be private behind NAT;
        making it reachable is the operator's job (the UI and CLI surface a
        hint on this tier).
    """
    # CLI entry points don't load config.env the way the server does; pull
    # it in here (existing environment values win) so every process derives
    # the same URL.
    import paths

    paths.load_config_env()

    base = normalize_base_url(os.getenv("MERLIN_DASHBOARD_URL", ""))
    if base:
        return base, "override"

    return discovered_public_base()


def discovered_public_base() -> tuple[str, str]:
    """The base URL the instance can work out on its own (no override).

    What applies when ``MERLIN_DASHBOARD_URL`` is unset — the Settings form
    shows it as the field's placeholder.
    """
    import paths

    paths.load_config_env()

    saas_host = _saas_public_host()
    if saas_host:
        return f"https://{saas_host}", "saas"

    slug = os.getenv("MERLIN_ENVIRONMENT_SLUG", "").strip()
    if slug:
        api = os.getenv("MERLIN_SAAS_API", "https://merlincloud.dev")
        host = urlparse(api).hostname or "merlincloud.dev"
        return f"https://{slug}.{host}", "slug"

    return f"http://{_local_ip()}:{_server_port()}", "ip"


def normalize_base_url(value: str) -> str:
    """Add ``http://`` to a scheme-less value and strip a trailing slash.

    Returns ``""`` for empty input. The single normalization rule shared by the
    read side (``resolve_public_base``) and the Settings write side
    (``main._normalize_public_url``, which adds host validation on top), so the
    same operator input renders identically everywhere.
    """
    value = value.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    return value.rstrip("/")


def _server_port() -> str:
    """The port the running server is bound to.

    Read from the server-state.json the server published; fall back to the
    legacy server-port file (pre-unified-state) during an upgrade; else the
    default. Lets `merlin job url` print the right port on a non-default one.
    """
    import paths

    state = paths.read_server_state()
    if state is not None:
        port = state.get("port")
        if isinstance(port, int):
            return str(port)

    try:
        persisted = paths.server_port_path().read_text().strip()
        if persisted:
            return persisted
    except OSError:
        pass
    return "3123"


def public_url(job_id: str) -> str:
    """The URL an external sender should call to fire this job's webhook."""
    base, _source = resolve_public_base()
    return f"{base}/webhooks/job/{job_id}"


def _local_ip() -> str:
    """Best-effort local IP (the interface that would route out)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
