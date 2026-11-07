"""Workspace restore API, read by the banner in ``templates/base.html``.

The framework mounts ``api_router`` at ``/api/workspace`` behind the dashboard
auth, and serves ``STATIC_DIR`` at ``/static/workspace`` (``restore.js``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException

from . import restore as rs
from . import service

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
async def api_dismiss():
    await asyncio.to_thread(service.dismiss_pending)
    return {"ok": True}
