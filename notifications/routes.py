"""Notifications API and the glue between the watcher and the sender.

Mounted by ``mount_module`` at ``/api/notifications`` (authed, so it works with
the local cookie and the Merlin Cloud proxy header alike) with ``STATIC_DIR``
at ``/static/notifications``. The attention events themselves ride the
sessions panel's poll (``GET /api/board?since=``), not a route here.

This is the only file that knows both sides: it builds the sender with the
terminal's "which windows are displayed" answer, and subscribes it to the
watcher. The watcher and the sender never import each other.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import paths

from . import watcher as _watcher
from .push import PushSender, SubscriptionStore, VapidKeys

logger = logging.getLogger("merlin.notifications")

STATIC_DIR = Path(__file__).parent.resolve() / "static"

api_router = APIRouter()


# ---------------------------------------------------------------------------
# The sender, built for the current Merlin home
# ---------------------------------------------------------------------------

_sender: PushSender | None = None
_sender_home: Path | None = None
_sender_lock = threading.Lock()


def notifications_dir() -> Path:
    return paths.merlin_home() / "notifications"


def _is_displayed(target: str) -> bool:
    # Imported here: terminal.routes imports the board package, which is also
    # where the watcher's sweep lives. Keeping it lazy avoids an import cycle.
    from terminal.routes import is_displayed

    return is_displayed(target)


def get_sender() -> PushSender:
    """The one sender per Merlin home (tests point the home elsewhere)."""
    global _sender, _sender_home
    home = notifications_dir()
    with _sender_lock:
        if _sender is None or _sender_home != home:
            _sender = PushSender(
                SubscriptionStore(home / "subscriptions.json"),
                VapidKeys(home / "vapid.json"),
                is_displayed=_is_displayed,
            )
            _sender_home = home
        return _sender


def _on_event(event: _watcher.Event) -> None:
    """Watcher listener: schedule the push on the running loop. Called from
    the watcher's tick, which runs on the event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(get_sender().deliver(event))


def wire_push() -> None:
    """Subscribe the sender to the watcher (once, at startup)."""
    if _on_event not in _watcher.watcher._listeners:
        _watcher.watcher.add_listener(_on_event)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@api_router.get("/status")
def api_status():
    """What the watcher sees: whether the last sweep found a tmux server, the
    current cursor, and how many devices are subscribed."""
    w = _watcher.watcher
    return {
        "tmux": w.tmux_available,
        "swept": w.swept_once,
        "cursor": w.cursor,
        "devices": len(get_sender().store.all()),
    }


@api_router.get("/public-key")
def api_public_key():
    """The VAPID application server key for ``PushManager.subscribe``."""
    return {"key": get_sender().keys.public_key}


class SubscribeReq(BaseModel):
    subscription: dict
    label: str = ""


class UnsubscribeReq(BaseModel):
    endpoint: str


class TestReq(BaseModel):
    endpoint: str = ""


_UA_BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("FxiOS/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_UA_PLATFORMS = (
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("Windows", "Windows"),
    ("Mac OS X", "Mac"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)


def device_label(user_agent: str) -> str:
    """A short device label from a user agent, for the devices list."""
    ua = user_agent or ""
    platform = next((label for key, label in _UA_PLATFORMS if key in ua), "")
    browser = next((label for key, label in _UA_BROWSERS if key in ua), "")
    parts = [p for p in (platform, browser) if p]
    return " · ".join(parts) if parts else "Device"


@api_router.post("/subscribe")
def api_subscribe(req: SubscribeReq, request: Request):
    """Register this browser's push subscription."""
    label = re.sub(r"\s+", " ", req.label).strip()[:80] or device_label(
        request.headers.get("user-agent", "")
    )
    try:
        sub = get_sender().store.add(req.subscription, label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "device": sub.public()}


@api_router.delete("/subscribe")
def api_unsubscribe(req: UnsubscribeReq):
    """Forget a subscription (this browser's, or any device from the list)."""
    removed = get_sender().store.remove(req.endpoint)
    return {"ok": True, "removed": removed}


@api_router.get("/devices")
def api_devices():
    return {"devices": [s.public() for s in get_sender().store.all()]}


@api_router.post("/test")
async def api_test(req: TestReq):
    """Send a test push to one device, or to all of them. Bypasses the
    suppression rules: the user asked for it."""
    sender = get_sender()
    payload = {
        "title": "Merlin · test",
        "body": "Push works on this device",
        "tag": "merlin-test",
        "url": "/terminal",
        "sid": "",
        "state": "test",
    }
    # The store may read its file here: off the event loop, like every send.
    result = await asyncio.to_thread(sender.test_sync, payload, req.endpoint)
    if result == "unknown":
        raise HTTPException(status_code=404, detail="Unknown device")
    if result == "none":
        raise HTTPException(status_code=409, detail="No device is subscribed")
    assert not isinstance(result, str)
    return {
        "ok": result.sent > 0,
        "sent": result.sent,
        "failed": result.failed,
        "removed": result.removed,
    }
