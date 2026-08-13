"""Build the session switcher's view model from a live tmux sweep.

One pure function, easy to test without a real tmux: ``build_tree`` turns the
session sweep + window sweep into the JSON the switcher renders. The model is a
**faithful view of tmux** plus an agent-activity overlay:

- Every tmux **session** is shown, grouped over every tmux **window** it holds
  (not only windows running an agent). A window that carries ``@agent_state``
  gets an activity dot (○ idle / ◐ busy / ? ask / ● done); a plain window shows
  without one. This is the deliberate reversal of the old board, which surfaced
  only agent windows.
- Windows keep tmux's own order (window index). ``--child`` fork/handoff windows
  nest one indent under their parent within the same session; siblings are flat.
- Per-session counts and a global attention total drive the badges.

Names come from tmux (``session_name`` / ``window_name``), so there is no
durable store to reconcile: the switcher renders what tmux actually holds.
"""

from __future__ import annotations

import os

from .sweep import TmuxSession, Window

_DONE = "done"
_BUSY = "busy"
_ASK = "ask"


def _project_of(cwd: str) -> str:
    return os.path.basename(cwd.rstrip("/")) if cwd else ""


def _window_node(w: Window, depth: int) -> dict:
    return {
        "sid": w.sid,
        "window_id": w.window_id,
        "index": w.index,
        "session": w.session,
        "name": w.name,
        "state": w.state,  # "" when the window runs no agent
        "is_agent": w.is_agent,
        "waiting": w.state == _DONE,
        "busy": w.state == _BUSY,
        "asking": w.state == _ASK,
        "active": w.active,
        "project": _project_of(w.cwd),
        "cwd": w.cwd,
        "relation": w.relation,
        "depth": depth,
    }


def _window_nodes(wins: list[Window]) -> list[dict]:
    """Order a session's windows by tmux index, nesting ``--child`` windows one
    indent under their parent (preorder). Siblings stay flat peers."""
    shown_sids = {w.sid for w in wins if w.sid}
    children_of: dict[str, list[Window]] = {}
    roots: list[Window] = []
    for w in wins:
        if w.relation == "child" and w.parent and w.parent in shown_sids:
            children_of.setdefault(w.parent, []).append(w)
        else:
            roots.append(w)

    rows: list[dict] = []

    def emit(w: Window, depth: int) -> None:
        rows.append(_window_node(w, depth))
        for kid in sorted(children_of.get(w.sid, []), key=lambda k: k.index):
            emit(kid, depth + 1)

    for w in sorted(roots, key=lambda w: w.index):
        emit(w, 0)
    return rows


def build_tree(
    sessions: list[TmuxSession],
    windows: list[Window],
    current_session: str,
    now: float,
) -> dict:
    """Build the switcher's JSON: sessions in tmux order, each with its windows.

    ``current_session`` is the session *this* client is attached to (the browser
    passes it, learned over the terminal WebSocket); it flags the current session
    so the UI can mark it without affecting any other client. Session order is
    tmux's own (typically by name), never by activity, so slots stay put.
    """
    by_session: dict[str, list[Window]] = {}
    for w in windows:
        by_session.setdefault(w.session, []).append(w)

    session_rows: list[dict] = []
    total_waiting = 0
    total_working = 0
    total_asking = 0
    for s in sessions:
        wins = by_session.get(s.name, [])
        counts = {
            "total": len(wins),
            "working": sum(1 for w in wins if w.state == _BUSY),
            "waiting": sum(1 for w in wins if w.state == _DONE),
            "asking": sum(1 for w in wins if w.state == _ASK),
        }
        total_waiting += counts["waiting"]
        total_working += counts["working"]
        total_asking += counts["asking"]
        session_rows.append(
            {
                "name": s.name,
                "attached": s.attached,
                "current": s.name == current_session,
                "counts": counts,
                "windows": _window_nodes(wins),
            }
        )

    return {
        "generated_at": now,
        "current_session": current_session,
        # Both states want you, so both count. They are kept apart below because
        # they are not equally urgent: 'asking' blocks a live turn (the agent has
        # stopped mid-work and cannot continue), 'waiting' is merely unread.
        "attention": total_waiting + total_asking,
        "counts": {
            "sessions": len(sessions),
            "waiting": total_waiting,
            "working": total_working,
            "asking": total_asking,
        },
        "sessions": session_rows,
    }
