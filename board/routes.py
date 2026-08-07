"""Sessions board — a 2D overview of every parallel agent session.

The framework mounts ``api_router`` at ``/api/board`` and ``page_router`` at
``/board`` (``URL_SLUG = "board"``). See ``docs/dev/dashboard-architecture.md``.

The board reads the live agent-state signal from tmux (``sweep``) and joins it
with durable per-session metadata (``store``): user names, manual order, pinned
launch cwd, family links, tombstones. GET returns the reconciled view; the POST
endpoints are the user-owned mutations (name, reorder, dismiss, focus).
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from merlin_ext import make_templates
from pydantic import BaseModel

from . import model, store, sweep


class NameReq(BaseModel):
    sid: str
    name: str | None = None


class OrderReq(BaseModel):
    sids: list[str]


class SidReq(BaseModel):
    sid: str


URL_SLUG = "board"

BOARD_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BOARD_DIR / "static"
templates = make_templates(BOARD_DIR / "templates")

api_router = APIRouter()
page_router = APIRouter()


@page_router.get("", response_class=HTMLResponse)
def board_page(request: Request):
    return templates.TemplateResponse(request, "board.html", {})


@api_router.get("")
def api_board():
    """The reconciled board view. Runs a sweep, folds it into the store (which
    applies the vanish/tombstone stop policy), and returns the view model."""
    now = time.time()
    with store.transaction() as st:
        windows = sweep.run_sweep()
        model.reconcile(st, windows, now)
        return model.build_view(st, windows, now)


@api_router.post("/name")
def api_set_name(req: NameReq):
    """Set (or clear, with an empty/blank name) a session's custom name."""
    cleaned = (req.name or "").strip()
    with store.transaction() as st:
        rec = st.sessions.get(req.sid)
        if rec is None:
            raise HTTPException(status_code=404, detail="Unknown session")
        rec.name = cleaned or None
    return {"ok": True, "name": cleaned or None}


@api_router.post("/order")
def api_reorder(req: OrderReq):
    """Assign manual order from a full ordered list of sids. Position in the
    list becomes the sort key, so the board keeps this arrangement across
    refreshes and never reorders on its own."""
    with store.transaction() as st:
        for index, sid in enumerate(req.sids):
            rec = st.sessions.get(sid)
            if rec is not None:
                rec.order = float(index)
    return {"ok": True}


@api_router.post("/dismiss")
def api_dismiss(req: SidReq):
    """Dismiss a tombstone (a session that died mid-work), removing its slot."""
    with store.transaction() as st:
        st.sessions.pop(req.sid, None)
    return {"ok": True}


@api_router.post("/focus")
def api_focus(req: SidReq):
    """Jump to a session's tmux window. Uses a fresh sweep so a just-moved
    window is still found."""
    windows = sweep.run_sweep()
    target = next((w for w in windows if w.sid == req.sid and w.is_agent), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Session is not live")
    if not sweep.focus_window(target.session, target.window_id):
        raise HTTPException(status_code=502, detail="Could not focus window")
    return {"ok": True}
