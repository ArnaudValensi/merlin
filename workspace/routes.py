"""Workspace restore API, read by the banner in ``templates/base.html``.

The framework mounts ``api_router`` at ``/api/workspace`` behind the dashboard
auth, and serves ``STATIC_DIR`` at ``/static/workspace`` (``restore.js``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from . import restore as rs
from . import service
from . import store

URL_SLUG = "workspace"

STATIC_DIR = Path(__file__).parent.resolve() / "static"

api_router = APIRouter()


@api_router.get("/pending")
async def api_pending():
    return await asyncio.to_thread(service.pending_info)


@api_router.post("/restore")
async def api_restore():
    try:
        return await asyncio.to_thread(service.restore_pending)
    except rs.RestoreBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except rs.RestoreUnreadable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@api_router.post("/dismiss")
async def api_dismiss(request: Request):
    """Drop the offer. A body ``{"id": ...}`` names the offer the page saw,
    so a newer one published meanwhile stays; no body (an older page) drops
    whatever is there."""
    examined: object = store.ANY_OFFER
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict) and "id" in body:
        examined = body["id"]
    await asyncio.to_thread(service.dismiss_pending, examined)
    return {"ok": True}
