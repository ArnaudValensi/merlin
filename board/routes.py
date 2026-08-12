"""Session switcher — the data + mutations behind the terminal's Sessions panel.

The framework mounts ``api_router`` at ``/api/board`` and serves ``STATIC_DIR``
at ``/static/board`` (``board.css`` / ``board.js``). There is no page of its
own: the switcher renders as a panel inside the web terminal (``terminal.html``),
where the sessions actually live. See ``docs/dev/dashboard-architecture.md``.

The switcher is a faithful view of tmux's session -> window tree plus an
agent-activity overlay (``sweep`` + ``model.build_tree``). GET returns the tree;
the POST endpoints are the **global** tmux mutations (create/rename/kill a
session, rename/kill a window). Switching and jump-to-window are *per-client* and
so live on the terminal WebSocket (``terminal/routes.py``), not here: only the
socket knows which tmux client this browser is.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import model, sweep


class NewSessionReq(BaseModel):
    name: str | None = None
    dir: str | None = None  # optional; defaults to the portal's working directory


class RenameSessionReq(BaseModel):
    name: str
    new: str


class SessionReq(BaseModel):
    name: str


class RenameWindowReq(BaseModel):
    session: str
    window_id: str
    name: str


class WindowReq(BaseModel):
    session: str
    window_id: str


class ReorderReq(BaseModel):
    session: str
    order: list[str]


URL_SLUG = "board"

BOARD_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BOARD_DIR / "static"

api_router = APIRouter()


@api_router.get("")
def api_board(current: str = ""):
    """The session -> window tree. Runs the session and window sweeps and builds
    the view. ``current`` is the session this client is on (the browser learns it
    over the terminal WebSocket and passes it back), used only to mark the
    current session; it never affects another client."""
    now = time.time()
    sessions = sweep.run_session_sweep()
    windows = sweep.run_sweep()
    return model.build_tree(sessions, windows, current, now)


@api_router.post("/session/new")
def api_new_session(req: NewSessionReq):
    """Create-or-switch: ensure a detached session named ``name`` (rooted at
    ``dir``, defaulting to the portal's cwd) exists and return its name. The
    browser then attaches to it with a per-client switch over the WebSocket."""
    directory = (req.dir or "").strip() or os.getcwd()
    name = sweep.create_or_get_session(directory, req.name or "")
    if name is None:
        raise HTTPException(status_code=502, detail="Could not create session")
    return {"ok": True, "name": name}


@api_router.post("/session/rename")
def api_rename_session(req: RenameSessionReq):
    """Rename a tmux session."""
    if not sweep.rename_session(req.name, req.new):
        raise HTTPException(status_code=502, detail="Could not rename session")
    return {"ok": True, "name": sweep.sanitize_session_name(req.new)}


@api_router.post("/session/kill")
def api_kill_session(req: SessionReq):
    """Kill an entire tmux session. Refuses to kill the last remaining session so
    a client is never left with nowhere to attach."""
    sessions = sweep.run_session_sweep()
    if len(sessions) <= 1:
        raise HTTPException(status_code=409, detail="Cannot close the last session")
    if req.name not in {s.name for s in sessions}:
        raise HTTPException(status_code=404, detail="Unknown session")
    if not sweep.kill_session(req.name):
        raise HTTPException(status_code=502, detail="Could not close session")
    return {"ok": True}


@api_router.post("/window/new")
def api_new_window(req: SessionReq):
    """Open a new window in ``name`` (a session) and return its id, so the
    browser can jump to it with a per-client switch."""
    wid = sweep.new_window(req.name)
    if wid is None:
        raise HTTPException(status_code=502, detail="Could not open window")
    return {"ok": True, "window_id": wid}


@api_router.post("/window/reorder")
def api_reorder_windows(req: ReorderReq):
    """Reorder a session's windows to the given order (real tmux swap-window)."""
    if not sweep.reorder_windows(req.session, req.order):
        raise HTTPException(status_code=502, detail="Could not reorder windows")
    return {"ok": True}


@api_router.post("/window/rename")
def api_rename_window(req: RenameWindowReq):
    """Rename a tmux window (its tab title)."""
    if not sweep.rename_window(req.session, req.window_id, req.name):
        raise HTTPException(status_code=502, detail="Could not rename window")
    return {"ok": True}


@api_router.post("/window/kill")
def api_kill_window(req: WindowReq):
    """Close a single tmux window."""
    if not sweep.kill_window(req.session, req.window_id):
        raise HTTPException(status_code=502, detail="Could not close window")
    return {"ok": True}
