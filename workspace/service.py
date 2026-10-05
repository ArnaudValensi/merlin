"""The background sweep and the pending-restore offer.

At server start, ``start()`` first freezes the restore offer (see ``store``),
then runs the sweep: every ``SWEEP_INTERVAL`` seconds it snapshots tmux and
writes ``latest.json`` only when the workspace changed. It never writes when
there is no tmux server or it holds no session, so a dead server never erases
the last good snapshot. A power cut loses at most one interval of changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading

from . import restore as rs
from . import snapshot as snap
from . import store

logger = logging.getLogger("merlin.workspace")

SWEEP_INTERVAL = 15.0

# Restore and Dismiss race the sweep and each other only through the pending
# file; one lock keeps a double click from restoring twice.
_restore_lock = threading.Lock()


def freeze_pending() -> bool:
    """Set the restore offer aside before anything overwrites ``latest.json``.

    Keeps an existing offer (Merlin restarted again before the user acted).
    Otherwise copies ``latest.json`` when sessions it holds are missing from
    tmux, counting the terminal's fresh ``merlin-dev`` placeholder as missing
    (``restore.missing_sessions``). Returns True when an offer is pending.
    """
    if store.read_snapshot(store.pending_path()) is not None:
        return True
    latest = store.read_snapshot(store.latest_path())
    if latest is None or not latest["sessions"]:
        return False
    if not rs.missing_sessions(latest, rs.live_sessions()):
        return False
    store.write_snapshot(store.pending_path(), latest)
    logger.info("Workspace restore offer frozen (%d sessions)", len(latest["sessions"]))
    return True


def sweep_once(last: dict | None) -> dict | None:
    """One sweep. Returns the snapshot now on disk (``last`` if unchanged)."""
    current = snap.take_snapshot()
    if current is None or not current["sessions"]:
        return last
    if snap.same_content(current, last):
        return last
    store.write_snapshot(store.latest_path(), current)
    return current


def pending_info() -> dict:
    """What the banner shows: the sessions a restore would bring back now.

    Recomputed against live tmux on every call. When nothing is missing any
    more (the user rebuilt it by hand), the offer is dropped.
    """
    pending = store.read_snapshot(store.pending_path())
    if pending is None:
        return {"pending": False}
    missing = rs.missing_sessions(pending, rs.live_sessions())
    if not missing:
        store.delete_pending()
        return {"pending": False}
    windows, agents = snap.counts(missing)
    return {
        "pending": True,
        "saved_at": pending.get("saved_at"),
        "windows": windows,
        "agents": agents,
        "sessions": [
            {
                "name": s["name"],
                "windows": len(s["windows"]),
                "agents": snap.counts([s])[1],
            }
            for s in missing
        ],
    }


def restore_pending() -> dict:
    """Run the restore of the pending offer, then drop the offer."""
    with _restore_lock:
        pending = store.read_snapshot(store.pending_path())
        if pending is None:
            return {"restored": [], "skipped": [], "failed": [], "agents": 0}
        try:
            return rs.restore(pending)
        finally:
            store.delete_pending()


def dismiss_pending() -> None:
    with _restore_lock:
        store.delete_pending()


async def run(stop: asyncio.Event, interval: float = SWEEP_INTERVAL) -> None:
    """Freeze the offer, then sweep until ``stop`` is set. Never raises.

    No sweep writes until the freeze has succeeded: a failed freeze (a full
    disk, say) is retried every interval, and ``latest.json``, the only copy of
    the restore point until then, is left alone.
    """
    frozen = False
    last = None
    logger.info("Workspace snapshot sweep started (every %.0fs)", interval)
    while not stop.is_set():
        if not frozen:
            try:
                await asyncio.to_thread(freeze_pending)
                frozen = True
                last = store.read_snapshot(store.latest_path())
            except Exception:
                logger.warning(
                    "Workspace restore: freezing the offer failed", exc_info=True
                )
        if frozen:
            try:
                last = await asyncio.to_thread(sweep_once, last)
            except Exception:
                logger.warning("Workspace snapshot sweep failed", exc_info=True)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


_task: asyncio.Task | None = None
_stop: asyncio.Event | None = None


def start() -> asyncio.Task:
    """Start the sweep as a background task (once)."""
    global _task, _stop
    if _task is not None and not _task.done():
        return _task
    _stop = asyncio.Event()
    _task = asyncio.create_task(run(_stop), name="workspace-sweep")
    return _task


async def stop(timeout: float = 6.0) -> None:
    global _task, _stop
    task, event = _task, _stop
    _task = _stop = None
    if task is None or event is None:
        return
    event.set()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except TimeoutError:
        task.cancel()
    except asyncio.CancelledError:
        task.cancel()
        raise
    except Exception:
        logger.exception("Workspace sweep ended with an error")
