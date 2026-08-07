"""Reconcile the tmux sweep with the durable store, then build the view model.

Two pure functions, both easy to test without a real tmux:

- ``reconcile(store, windows, now)`` folds a fresh sweep into the store: it
  upserts live agent sessions, and decides what happens to sessions that
  vanished — the stop policy. Intentional close (idle/done at disappearance) ->
  the record is dropped (vanish). Died while busy -> a dismissible tombstone.

- ``build_view(store, windows, now)`` turns the reconciled store + sweep into
  the JSON the board renders: sessions grouped by project, families nested by
  parent (hierarchy wins placement), stable order, plus the plain-window tier
  and the attention count. It never reorders by state.
"""

from __future__ import annotations

import os

from .store import Session, Store
from .sweep import Window

_DONE = "done"
_BUSY = "busy"


def _project_of(cwd: str) -> str:
    return os.path.basename(cwd.rstrip("/")) if cwd else ""


def reconcile(store: Store, windows: list[Window], now: float) -> None:
    """Fold a sweep into the store in place (see module docstring)."""
    live_agents = {w.sid: w for w in windows if w.is_agent and w.sid}

    for sid, w in live_agents.items():
        rec = store.sessions.get(sid)
        if rec is None:
            rec = Session(sid=sid, first_seen=now)
            store.sessions[sid] = rec
        # Pin identity fields on first sight; never let a later sweep move them.
        if not rec.cwd and w.cwd:
            rec.cwd = w.cwd
            rec.project = _project_of(w.cwd)
        if not rec.parent and w.parent:
            rec.parent = w.parent
        if not rec.relation and w.relation:
            rec.relation = w.relation
        # Live fields: always refreshed from the sweep.
        rec.last_seen = now
        rec.state = w.state or "idle"
        rec.live = True
        rec.session = w.session
        rec.window_id = w.window_id
        rec.closed_at = None
        rec.tombstone = False

    # Sessions that were live last time but are gone now: apply the stop policy.
    for sid, rec in list(store.sessions.items()):
        if sid in live_agents:
            continue
        if not rec.live:
            continue  # already closed / already a tombstone
        rec.live = False
        rec.session = ""
        rec.window_id = ""
        rec.closed_at = now
        if rec.state == _BUSY:
            rec.tombstone = True  # died mid-work -> keep a mark
        else:
            del store.sessions[sid]  # idle/done -> intentional close -> vanish


def _display_name(rec: Session) -> str:
    if rec.name:
        return rec.name
    return rec.project or "session"


def _sort_key(rec: Session) -> tuple[float, float]:
    # Stable position: explicit manual order first, else first-seen. Never state.
    order = rec.order if rec.order is not None else rec.first_seen
    return (order, rec.first_seen)


def _node(rec: Session, active_win: str, depth: int, children: list[dict]) -> dict:
    return {
        "sid": rec.sid,
        "name": _display_name(rec),
        "custom_name": rec.name,
        "auto_name": rec.project or "session",
        "short_id": rec.sid[:4],
        "state": rec.state,
        "waiting": rec.live and rec.state == _DONE,
        "busy": rec.live and rec.state == _BUSY,
        "live": rec.live,
        "tombstone": rec.tombstone,
        "closed_at": rec.closed_at,
        "cwd": rec.cwd,
        "project": rec.project,
        "relation": rec.relation,
        "active": bool(rec.live and rec.window_id and rec.window_id == active_win),
        "session": rec.session,
        "window_id": rec.window_id,
        "depth": depth,
        "children": children,
    }


def build_view(store: Store, windows: list[Window], now: float) -> dict:
    """Build the board's JSON view model from the reconciled store + sweep."""
    shown = {
        sid: rec for sid, rec in store.sessions.items() if rec.live or rec.tombstone
    }
    active_win = next((w.window_id for w in windows if w.active and w.is_agent), "")

    # Only a `child` relation nests under its parent (one indent). Siblings (the
    # fork/handoff default) stay flat as peers — they are roots, grouped by their
    # own project, exactly like the session they were spawned from. A `child`
    # whose parent we are not showing falls back to a root.
    children_of: dict[str, list[Session]] = {}
    roots: list[Session] = []
    for rec in shown.values():
        if rec.relation == "child" and rec.parent and rec.parent in shown:
            children_of.setdefault(rec.parent, []).append(rec)
        else:
            roots.append(rec)

    def subtree(rec: Session, depth: int) -> dict:
        kids = sorted(children_of.get(rec.sid, []), key=_sort_key)
        return _node(rec, active_win, depth, [subtree(k, depth + 1) for k in kids])

    # Group roots by project, preserving a stable project order (first appearance
    # by sort key), and stable session order within each project.
    projects: dict[str, dict] = {}
    for rec in sorted(roots, key=_sort_key):
        key = rec.project or "~"
        bucket = projects.setdefault(
            key, {"project": rec.project, "cwd": rec.cwd, "sessions": []}
        )
        bucket["sessions"].append(subtree(rec, 0))

    other = [
        {
            "window_id": w.window_id,
            "session": w.session,
            "name": w.name,
            "active": w.active,
        }
        for w in windows
        if not w.is_agent
    ]

    attention = sum(1 for rec in shown.values() if rec.live and rec.state == _DONE)

    return {
        "generated_at": now,
        "attention": attention,
        "projects": list(projects.values()),
        "other_windows": other,
    }
