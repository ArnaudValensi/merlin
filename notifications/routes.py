"""Notifications API and assets. Mounted by ``mount_module`` at
``/api/notifications`` (authed) with ``STATIC_DIR`` at ``/static/notifications``.

The attention events themselves ride the sessions panel's poll
(``GET /api/board?since=``), not a route here. This router holds what the
popover needs from the server: the watcher's status now, the push endpoints in
a later milestone.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from . import watcher as _watcher

STATIC_DIR = Path(__file__).parent.resolve() / "static"

api_router = APIRouter()


@api_router.get("/status")
def api_status():
    """What the watcher sees: whether the last sweep found a tmux server, and
    the current cursor."""
    w = _watcher.watcher
    return {
        "tmux": w.tmux_available,
        "swept": w.swept_once,
        "cursor": w.cursor,
    }
