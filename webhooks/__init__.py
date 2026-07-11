"""Webhooks front desk — the one public, secret-gated HTTP surface.

Core module. A single parameterized route ``POST /webhooks/{source}/{target_id}``
owns everything shared and security-sensitive about inbound webhooks: secret
verification (constant-time) and logging. It then dispatches by ``source`` to a
resolver each module registers.

Security model: the per-hook secret is the gate, and a valid secret is checked
BEFORE anything else that could reject a request, so a legitimate sender can
never be locked out. There is deliberately no in-app rate limiter — the secret
is 256-bit (unguessable at any rate), so throttling failed guesses buys nothing
for security. Bounding the cost of a junk flood is a job for the edge (our
Caddy on SaaS; the operator's own reverse proxy when self-hosting), consistent
with Merlin's bring-your-own-exposure model.

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
import uuid
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
# reach a module's filesystem lookup (path traversal). fullmatch (not match)
# so a trailing newline can't slip through the ``$`` anchor.
_ID_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_ID_MAX_LEN = 30


@dataclass
class FireResult:
    """What a module's ``fire`` callback reports back to the desk."""

    status: str  # "launched" | "coalesced"
    run_id: str | None = None


@dataclass
class WebhookTarget:
    """A resolved webhook target: the expected secret and how to fire it.

    ``fire`` is called only after the secret check passes. It must return
    quickly (launch asynchronously) — the HTTP response does not wait for the
    run to finish.
    """

    secret: str
    fire: Callable[[], FireResult]
    enabled: bool = True


Resolver = Callable[[str], WebhookTarget | None]

_registry: dict[str, Resolver] = {}


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
# Client IP (for logging only)
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """Best-effort client IP for the request log's ``ip`` field.

    Informational only — it is NOT used for any access decision. The first
    X-Forwarded-For hop is caller-controlled (spoofable), and behind the SaaS
    tunnel the socket peer is just the tunnel's local end, so this is a hint
    for humans reading the audit log, never a gate.
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
    secret, 403 disabled target, 404 unknown source/target.

    A valid secret is verified before the enabled check and is never gated by
    any per-caller state, so a legitimate sender cannot be locked out. Flood
    protection is an edge concern (see the module docstring), not handled here.
    """
    request_id = str(uuid.uuid4())
    ip = _client_ip(request)

    def _log(outcome: str, target: str = target_id, **fields) -> None:
        log_event(
            "webhook_request",
            source=source[:64],
            target=target,
            ip=ip,
            outcome=outcome,
            request_id=request_id,
            **fields,
        )

    resolver = _registry.get(source)
    if resolver is None:
        _log("unknown_source", target="")
        raise HTTPException(status_code=404, detail="Unknown webhook")

    if len(target_id) > _ID_MAX_LEN or not _ID_RE.fullmatch(target_id):
        # Don't echo an arbitrary path segment into the log.
        _log("invalid_id", target="")
        raise HTTPException(status_code=404, detail="Unknown webhook")

    target = resolver(target_id)
    if target is None:
        _log("unknown_target")
        raise HTTPException(status_code=404, detail="Unknown webhook")

    provided = request.headers.get(SECRET_HEADER) or token or ""
    if not hmac.compare_digest(provided.encode(), target.secret.encode()):
        _log("rejected_secret")
        logger.warning("Webhook secret rejected: ip=%s %s/%s", ip, source, target_id)
        raise HTTPException(status_code=401, detail="Invalid secret")

    if not target.enabled:
        _log("disabled")
        raise HTTPException(status_code=403, detail="Webhook target is disabled")

    result = target.fire()
    _log(result.status, run_id=result.run_id)
    logger.info(
        "Webhook %s: %s/%s (ip=%s, run_id=%s)",
        result.status,
        source,
        target_id,
        ip,
        result.run_id,
    )
    return {"ok": True, "status": result.status, "run_id": result.run_id}
