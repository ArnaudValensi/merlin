"""Web Push: the VAPID keys, the subscription store and the sender.

Standard RFC 8030 push with VAPID, sent through ``pywebpush`` (synchronous,
so every send runs in a thread). This module takes an attention event and a
store and knows nothing about tmux, which is the seam the hub epic reuses:
the same sender runs on the hub with events relayed from nodes.

Files, both mode 0600 under ``~/.merlin/notifications/``:

- ``vapid.json``: the key pair, generated on first use.
- ``subscriptions.json``: one entry per endpoint with a device label, a
  created timestamp and a last-success timestamp.

A store file that fails to parse, or holds the wrong shape, is moved aside
under another name and the store starts empty. Nothing here raises into the
watcher: a send that fails is logged with ``log_event``, and a 404 or 410
removes the subscription.

Threads: route handlers run in FastAPI's worker threads and sends run in
``asyncio.to_thread``, so the stores are shared between threads. Every
read-modify-write of a store runs whole under its lock, the key pair is
generated once under a lock, and files are written through a unique temp
file in the destination directory before an atomic rename. Async entry
points never touch a store on the event loop.

Suppression, push only: no push when a connected terminal client currently
displays the event's window (the caller says which windows are displayed),
and no second push for the same sid within 20 seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from .watcher import Event

logger = logging.getLogger("merlin.notifications")

TTL_SECONDS = 300
URGENCY = "high"
MIN_INTERVAL_SECONDS = 20.0
PAYLOAD_LIMIT = 3 * 1024
VAPID_SUBJECT = "mailto:merlin@localhost"


def _now_iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).isoformat()


def _write_private(path: Path, data: dict) -> None:
    """Write JSON atomically with mode 0600. The temp file is unique (two
    writers never share one name) and lives in the destination directory so
    the final rename is atomic."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_name, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    os.chmod(str(path), 0o600)


def _set_aside(path: Path, why: str) -> None:
    """Keep a bad store file under ``<name>.corrupt-<timestamp>`` and log it."""
    aside = path.with_name(f"{path.name}.corrupt-{int(time.time() * 1000)}")
    try:
        os.replace(str(path), str(aside))
    except OSError:
        pass
    logger.warning("Unreadable %s moved to %s: %s", path.name, aside.name, why)


def _read_private(path: Path) -> dict | None:
    """Read a JSON object. A file that does not parse, or is not an object, is
    set aside and None is returned, never an exception."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        _set_aside(path, str(exc))
        return None
    if not isinstance(data, dict):
        _set_aside(path, "not a JSON object")
        return None
    return data


# ---------------------------------------------------------------------------
# VAPID keys
# ---------------------------------------------------------------------------


class VapidKeys:
    """The application server key pair, generated on first use."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._private_pem: str | None = None
        self._public_key: str | None = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        # Under the lock: two first readers (a route thread and a send thread)
        # must agree on one pair, never generate two and keep the wrong one.
        with self._lock:
            if self._private_pem is not None:
                return
            data = _read_private(self.path)
            pem = data.get("private_pem") if data else None
            public = data.get("public_key") if data else None
            if (
                isinstance(pem, str)
                and pem.startswith("-----BEGIN")
                and isinstance(public, str)
                and public
            ):
                self._private_pem = pem
                self._public_key = public
                return
            if data is not None:
                _set_aside(self.path, "not a key pair")
            self._generate()

    def _generate(self) -> None:
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )
        from py_vapid import Vapid
        from py_vapid.utils import b64urlencode

        vapid = Vapid()
        vapid.generate_keys()
        raw = vapid.public_key.public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )
        self._private_pem = vapid.private_pem().decode()
        self._public_key = b64urlencode(raw)
        _write_private(
            self.path,
            {
                "private_pem": self._private_pem,
                "public_key": self._public_key,
                "created": datetime.now(tz=timezone.utc).isoformat(),
            },
        )
        logger.info("Generated the Web Push VAPID key pair at %s", self.path)

    @property
    def private_pem(self) -> str:
        self._load()
        assert self._private_pem is not None
        return self._private_pem

    @property
    def public_key(self) -> str:
        """The application server key, base64url of the uncompressed point,
        as ``PushManager.subscribe`` takes it."""
        self._load()
        assert self._public_key is not None
        return self._public_key

    def vapid(self) -> Any:
        """The key as ``pywebpush`` takes it (a ``Vapid`` object: the string
        form it accepts is raw base64, not PEM)."""
        from py_vapid import Vapid

        return Vapid.from_pem(self.private_pem.encode())


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


@dataclass
class Subscription:
    endpoint: str
    keys: dict[str, str]
    label: str
    created: str
    last_success: str = ""

    def info(self) -> dict:
        """The shape ``pywebpush`` takes."""
        return {"endpoint": self.endpoint, "keys": dict(self.keys)}

    def public(self) -> dict:
        """What the devices list shows (the keys stay in the file)."""
        return {
            "endpoint": self.endpoint,
            "label": self.label,
            "created": self.created,
            "last_success": self.last_success,
        }


class SubscriptionStore:
    """Subscriptions keyed by endpoint, persisted as one 0600 JSON file."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._subs: dict[str, Subscription] | None = None
        # Every read-modify-write runs whole under this lock: route threads
        # and send threads share the store.
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Subscription]:
        if self._subs is not None:
            return self._subs
        subs: dict[str, Subscription] = {}
        data = _read_private(self.path)
        raw_subs: Any = data.get("subscriptions") if data is not None else {}
        if not isinstance(raw_subs, dict):
            _set_aside(self.path, "subscriptions is not a mapping")
            raw_subs = {}
        for endpoint, raw in raw_subs.items():
            if not isinstance(raw, dict):
                continue
            keys = raw.get("keys")
            if (
                not isinstance(keys, dict)
                or not keys.get("p256dh")
                or not keys.get("auth")
            ):
                continue
            subs[str(endpoint)] = Subscription(
                endpoint=str(endpoint),
                keys={"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
                label=str(raw.get("label", "")),
                created=str(raw.get("created", "")),
                last_success=str(raw.get("last_success", "")),
            )
        self._subs = subs
        return subs

    def _save(self) -> None:
        subs = self._load()
        _write_private(
            self.path,
            {"subscriptions": {e: asdict(s) for e, s in subs.items()}},
        )

    def all(self) -> list[Subscription]:
        with self._lock:
            return sorted(self._load().values(), key=lambda s: s.created)

    def get(self, endpoint: str) -> Subscription | None:
        with self._lock:
            return self._load().get(endpoint)

    def add(self, info: dict, label: str) -> Subscription:
        """Add or refresh a subscription from a ``PushSubscription.toJSON()``.
        Raises ValueError when the shape is not a push subscription."""
        endpoint = str(info.get("endpoint", "")).strip()
        keys = info.get("keys") or {}
        if not endpoint.startswith("https://") and not endpoint.startswith("http://"):
            raise ValueError("endpoint must be a URL")
        if not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("subscription keys are missing")
        with self._lock:
            subs = self._load()
            existing = subs.get(endpoint)
            sub = Subscription(
                endpoint=endpoint,
                keys={"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
                label=(label or "").strip()[:80]
                or (existing.label if existing else "Device"),
                created=existing.created if existing else _now_iso(self._clock),
                last_success=existing.last_success if existing else "",
            )
            subs[endpoint] = sub
            self._save()
            return sub

    def remove(self, endpoint: str) -> bool:
        with self._lock:
            subs = self._load()
            if endpoint not in subs:
                return False
            del subs[endpoint]
            self._save()
            return True

    def mark_success(self, endpoint: str) -> None:
        with self._lock:
            sub = self._load().get(endpoint)
            if sub is None:
                return
            sub.last_success = _now_iso(self._clock)
            self._save()


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------

# Bounds on the user-controlled strings, so the payload is complete JSON well
# under the limit before any encryption.
_TITLE_MAX = 120
_TAG_MAX = 200


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def deep_link(target: str) -> str:
    """``/terminal?target=<session>:<window_id>``, the target query-encoded so
    a session name holding ``&`` or ``#`` survives ``URLSearchParams``."""
    return "/terminal?target=" + quote(target, safe="")


def build_payload(event: Event) -> dict:
    """The push payload the service worker shows. Complete JSON, strictly
    under 3 KB once encoded (``encode_payload`` checks)."""
    title = f"{event.project or event.session} · {event.window_name or 'window'}"
    return {
        "title": _clip(title, _TITLE_MAX),
        "body": "Needs an answer" if event.state == "ask" else "Finished",
        "tag": _clip(event.sid, _TAG_MAX),
        "url": deep_link(event.target),
        "sid": _clip(event.sid, _TAG_MAX),
        "state": event.state,
    }


def encode_payload(payload: dict) -> str:
    """Serialize a payload, shortening its strings structurally (never slicing
    the JSON) until the UTF-8 form is strictly under the limit."""
    limits = {"title": _TITLE_MAX, "body": 200, "tag": _TAG_MAX, "sid": _TAG_MAX}
    data = dict(payload)
    for key, limit in limits.items():
        if isinstance(data.get(key), str):
            data[key] = _clip(data[key], limit)
    for _ in range(8):
        text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        if len(text.encode("utf-8")) < PAYLOAD_LIMIT:
            return text
        # Still too big (a very long url or state): halve every string.
        for key, value in list(data.items()):
            if isinstance(value, str) and len(value) > 8:
                data[key] = _clip(value, max(8, len(value) // 2))
    return json.dumps(
        {"title": "Merlin", "body": str(data.get("body", "")), "url": "/terminal"},
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# The sender
# ---------------------------------------------------------------------------


@dataclass
class SendResult:
    sent: int = 0
    failed: int = 0
    removed: int = 0
    skipped: str = ""  # the suppression reason when nothing was attempted


def _default_send(**kwargs: Any) -> Any:
    from pywebpush import webpush

    return webpush(**kwargs)


class PushSender:
    """Send attention events to every subscription, with the two push-only
    suppression rules. ``is_displayed(target)`` is supplied by the caller (the
    terminal knows which windows its connected clients show, this module does
    not)."""

    def __init__(
        self,
        store: SubscriptionStore,
        keys: VapidKeys,
        *,
        is_displayed: Callable[[str], bool] = lambda _t: False,
        clock: Callable[[], float] = time.time,
        min_interval: float = MIN_INTERVAL_SECONDS,
        send: Callable[..., Any] = _default_send,
        subject: str = VAPID_SUBJECT,
    ) -> None:
        self.store = store
        self.keys = keys
        self.is_displayed = is_displayed
        self._clock = clock
        self.min_interval = min_interval
        self._send = send
        self.subject = subject
        self._last_push: dict[str, float] = {}
        self._reserve_lock = threading.Lock()

    # -- suppression --------------------------------------------------------

    def suppression_reason(self, event: Event) -> str:
        """Why this event gets no push, or an empty string. Read-only."""
        if self.is_displayed(event.target):
            return "displayed"
        with self._reserve_lock:
            last = self._last_push.get(event.sid)
        if last is not None and self._clock() - last < self.min_interval:
            return "recent"
        return ""

    def reserve(self, event: Event) -> str:
        """Check the two rules and, when nothing suppresses the event, reserve
        the sid for the rate limit in the same step. Atomic: two deliveries
        for one sid can never both pass. A suppressed event reserves nothing."""
        if self.is_displayed(event.target):
            return "displayed"
        with self._reserve_lock:
            last = self._last_push.get(event.sid)
            now = self._clock()
            if last is not None and now - last < self.min_interval:
                return "recent"
            self._last_push[event.sid] = now
            return ""

    # -- sending ------------------------------------------------------------

    def send_one(self, sub: Subscription, payload: dict) -> str:
        """Send one push synchronously. Returns ``sent``, ``removed`` or
        ``failed``. Never raises."""
        from structured_log import log_event

        data = encode_payload(payload)
        try:
            self._send(
                subscription_info=sub.info(),
                data=data,
                vapid_private_key=self.keys.vapid(),
                vapid_claims={"sub": self.subject},
                ttl=TTL_SECONDS,
                headers={"Urgency": URGENCY},
                timeout=10,
            )
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                self.store.remove(sub.endpoint)
                log_event(
                    "push_removed",
                    endpoint=sub.endpoint,
                    label=sub.label,
                    status=status,
                )
                return "removed"
            log_event(
                "push_failed",
                endpoint=sub.endpoint,
                label=sub.label,
                status=status,
                error=str(exc)[:300],
            )
            return "failed"
        self.store.mark_success(sub.endpoint)
        return "sent"

    def send_all_sync(
        self, payload: dict, only: Iterable[str] | None = None
    ) -> SendResult:
        result = SendResult()
        wanted = set(only) if only is not None else None
        for sub in self.store.all():
            if wanted is not None and sub.endpoint not in wanted:
                continue
            outcome = self.send_one(sub, payload)
            if outcome == "sent":
                result.sent += 1
            elif outcome == "removed":
                result.removed += 1
            else:
                result.failed += 1
        return result

    async def send_all(
        self, payload: dict, only: Iterable[str] | None = None
    ) -> SendResult:
        """Send off the event loop (``pywebpush`` is synchronous)."""
        return await asyncio.to_thread(self.send_all_sync, payload, only)

    def _has_subscriptions(self) -> bool:
        return bool(self.store.all())

    async def deliver(self, event: Event) -> SendResult:
        """Push one attention event, unless a suppression rule applies. The
        entry point the watcher's listener uses. Never raises.

        The store is read in a thread. The suppression check and the sid
        reservation run on the event loop with no await between them (and
        under a lock besides), so concurrent deliveries for one sid cannot
        both send, and ``is_displayed`` reads the terminal's registry on the
        thread that owns it."""
        try:
            if not await asyncio.to_thread(self._has_subscriptions):
                return SendResult(skipped="no subscriptions")
            reason = self.reserve(event)
            if reason:
                return SendResult(skipped=reason)
            return await self.send_all(build_payload(event))
        except Exception:
            logger.exception("Push delivery failed")
            return SendResult(skipped="error")

    def test_sync(self, payload: dict, endpoint: str = "") -> SendResult | str:
        """A test push to one device or to all, bypassing suppression. Returns
        the result, or ``"unknown"`` / ``"none"`` when there is nothing to
        send to. Runs off the loop like every other store access."""
        if endpoint and self.store.get(endpoint) is None:
            return "unknown"
        if not self.store.all():
            return "none"
        return self.send_all_sync(payload, [endpoint] if endpoint else None)
