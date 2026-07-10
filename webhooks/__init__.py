"""Webhooks front desk — the one public, secret-gated HTTP surface.

Core module. A single parameterized route ``POST /webhooks/{source}/{target_id}``
owns everything shared and security-sensitive about inbound webhooks: secret
verification (constant-time), the per-IP failed-secret throttle, and logging.
It then dispatches by ``source`` to a resolver each module registers.

The router is hand-mounted in ``main.py`` WITHOUT ``require_auth`` (the
terminal precedent): ``/webhooks/*`` is intentionally public and
self-authenticating via the per-hook secret, while everything under ``/api``
stays session-gated. The boundary is legible by URL.

Registration:
  - Core modules call ``webhooks.register(source, resolver)`` directly
    (``main.py`` registers the ``job`` source).
  - Extensions export ``WEBHOOK_HANDLERS = {source: resolver}``; the extension
    loader picks it up next to ``NAV_ITEMS``. Extensions cannot open their own
    unauthenticated route (the loader force-wraps their routers in auth), so
    the front desk is what makes an extension webhook possible at all.

A resolver maps a target id to a :class:`WebhookTarget` (or ``None`` if the id
is unknown). The front desk verifies the secret and only then calls
``target.fire()``. Handler logic (single-flight, session policy, launching)
lives in the owning module; the desk owns only the shared plumbing.
"""

from __future__ import annotations

import hmac
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, HTTPException, Request

from structured_log import log_event

logger = logging.getLogger("merlin.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Secret transport: header preferred, ?token= query fallback (for senders
# that cannot set custom headers).
SECRET_HEADER = "x-merlin-webhook-secret"

# Target ids share the job-id shape: lowercase, digits, single hyphens,
# starts with a letter. Enforced at the desk so a raw path segment can never
# reach a module's filesystem lookup (path traversal).
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_ID_MAX_LEN = 30

# Failed-secret throttle: this many failures from one IP inside the window
# puts the IP in cooldown (429) until failures age out. In-memory by design:
# single process, reset on restart is acceptable.
THROTTLE_MAX_FAILURES = 10
THROTTLE_WINDOW_SECONDS = 300


@dataclass
class FireResult:
    """What a module's ``fire`` callback reports back to the desk."""

    status: str  # "launched" | "coalesced"
    run_id: str | None = None


@dataclass
class WebhookTarget:
    """A resolved webhook target: the expected secret and how to fire it.

    ``fire`` is called only after the secret check and throttle pass. It must
    return quickly (launch asynchronously) — the HTTP response does not wait
    for the run to finish.
    """

    secret: str
    fire: Callable[[], FireResult]
    enabled: bool = True


Resolver = Callable[[str], WebhookTarget | None]

_registry: dict[str, Resolver] = {}

# Failed-secret timestamps per client IP (monotonic seconds).
_failures: dict[str, deque[float]] = {}

_now = time.monotonic


def register(source: str, resolver: Resolver) -> None:
    """Register a resolver for a webhook source. First registration wins.

    Refusing duplicates (instead of overwriting) means an installed extension
    can never hijack a source a core module already owns.
    """
    if source in _registry:
        logger.warning(
            "Webhook source %r already registered, ignoring duplicate", source
        )
        return
    _registry[source] = resolver
    logger.info("Webhook source registered: %s", source)


# ---------------------------------------------------------------------------
# Failed-secret throttle
# ---------------------------------------------------------------------------


def _prune_failures(ip: str) -> deque[float]:
    """Drop failures older than the window; return the IP's live deque."""
    q = _failures.setdefault(ip, deque())
    cutoff = _now() - THROTTLE_WINDOW_SECONDS
    while q and q[0] < cutoff:
        q.popleft()
    if not q:
        _failures.pop(ip, None)
    return q


def _is_throttled(ip: str) -> bool:
    return len(_prune_failures(ip)) >= THROTTLE_MAX_FAILURES


def _record_failure(ip: str) -> None:
    _failures.setdefault(ip, deque()).append(_now())


def _client_ip(request: Request) -> str:
    """Best-effort client IP: first X-Forwarded-For hop, else the socket peer.

    Behind the SaaS tunnel the socket peer is the tunnel's local end, so the
    forwarded header is the only useful signal there. It is spoofable by
    direct callers, which is acceptable for a brute-force throttle: a spoofing
    attacker only pollutes buckets, never bypasses the secret.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# The front desk route
# ---------------------------------------------------------------------------


@router.post("/{source}/{target_id}")
async def receive_webhook(
    source: str, target_id: str, request: Request, token: str | None = None
):
    """Verify and dispatch an inbound webhook. Public, secret-gated.

    Response codes: 200 accepted (launched or coalesced), 401 bad/missing
    secret, 403 disabled target, 404 unknown source/target, 429 throttled.
    """
    request_id = str(uuid.uuid4())
    ip = _client_ip(request)

    resolver = _registry.get(source)
    if resolver is None:
        log_event(
            "webhook_request",
            source=source[:64],
            target="",
            ip=ip,
            outcome="unknown_source",
            request_id=request_id,
        )
        raise HTTPException(status_code=404, detail="Unknown webhook")

    if len(target_id) > _ID_MAX_LEN or not _ID_RE.match(target_id):
        # Don't echo an arbitrary path segment into the log.
        log_event(
            "webhook_request",
            source=source,
            target="",
            ip=ip,
            outcome="invalid_id",
            request_id=request_id,
        )
        raise HTTPException(status_code=404, detail="Unknown webhook")

    # Cooldown check before any secret work: a throttled IP gets no oracle.
    if _is_throttled(ip):
        log_event(
            "webhook_request",
            source=source,
            target=target_id,
            ip=ip,
            outcome="throttled",
            request_id=request_id,
        )
        logger.warning("Webhook throttled: ip=%s %s/%s", ip, source, target_id)
        raise HTTPException(status_code=429, detail="Too many failed attempts")

    target = resolver(target_id)
    if target is None:
        log_event(
            "webhook_request",
            source=source,
            target=target_id,
            ip=ip,
            outcome="unknown_target",
            request_id=request_id,
        )
        raise HTTPException(status_code=404, detail="Unknown webhook")

    provided = request.headers.get(SECRET_HEADER) or token or ""
    if not hmac.compare_digest(provided.encode(), target.secret.encode()):
        _record_failure(ip)
        log_event(
            "webhook_request",
            source=source,
            target=target_id,
            ip=ip,
            outcome="rejected_secret",
            request_id=request_id,
        )
        logger.warning("Webhook secret rejected: ip=%s %s/%s", ip, source, target_id)
        raise HTTPException(status_code=401, detail="Invalid secret")

    if not target.enabled:
        log_event(
            "webhook_request",
            source=source,
            target=target_id,
            ip=ip,
            outcome="disabled",
            request_id=request_id,
        )
        raise HTTPException(status_code=403, detail="Webhook target is disabled")

    result = target.fire()
    log_event(
        "webhook_request",
        source=source,
        target=target_id,
        ip=ip,
        outcome=result.status,
        run_id=result.run_id,
        request_id=request_id,
    )
    logger.info(
        "Webhook %s: %s/%s (ip=%s, run_id=%s)",
        result.status,
        source,
        target_id,
        ip,
        result.run_id,
    )
    return {"ok": True, "status": result.status, "run_id": result.run_id}
