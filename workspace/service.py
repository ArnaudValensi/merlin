"""The background sweep, the death detection and the pending-restore offer.

At server start, ``start()`` first freezes the restore offer (see ``store``),
then runs the sweep: every ``SWEEP_INTERVAL`` seconds it snapshots tmux and
writes ``latest.json`` only when the workspace changed, with a copy in
``history/`` (thinned after each write). It never writes when there is no
tmux server or it holds no session, so a dead server never erases the last
good snapshot. A power cut loses at most one interval of changes.

An offer is a snapshot with its ``crash`` object (reason ``startup`` for the
freeze at startup) and the ``id`` of its crash point. The freeze publishes
the offer first (``pending-restore.json``, a full copy, under the offer
lock), then keeps the crash point under ``crashes/``. A failed offer write
leaves nothing, and the next sweep redoes the freeze. A failed crash write
leaves the published offer, and ``store.archive_pending`` completes its
crash point from the offer itself (same id, reason and detection time) at
startup, at the top of every sweep, and before the offer is consumed or
replaced, always before ``latest.json`` is written over.

**Death detection** runs in the sweep, before it writes, from the one set of
tmux reads it already makes. ``disk`` is the snapshot ``latest.json`` holds
and ``now`` the sweep's read:

- ``server-changed``: ``now`` read fine, both snapshots know their server
  (pid and start time) and they differ. The server ``disk`` was taken from is
  dead; the web terminal has typically recreated one already.
- ``server-gone``: ``NO_SERVER_SWEEPS`` consecutive sweeps found no server at
  all. One is not enough. A read that *failed* (a server answered, a list
  call errored) is not "gone" and counts toward nothing, neither way.

On either rule, if ``disk`` holds sessions missing from live tmux, it is
frozen as a crash point and becomes the offer (replacing a pending one: this
is a newer loss), and only then may the sweep write the new ``latest.json``.
A dead server is frozen once, keyed on its identity, which the crash points
on disk remember across a Merlin restart. Nothing is ever restored on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from . import restore as rs
from . import snapshot as snap
from . import store

logger = logging.getLogger("merlin.workspace")

SWEEP_INTERVAL = 15.0
# Consecutive sweeps without a server before the server counts as gone.
NO_SERVER_SWEEPS = 2
# A crash at night must still reach a phone that was off until the morning.
CRASH_PUSH_TTL = 12 * 3600
CRASH_PUSH_TAG = "workspace-restore"
RUNTIME_REASONS = ("server-changed", "server-gone")


def freeze(snapshot: dict, reason: str, now: float | None = None) -> dict:
    """Make ``snapshot`` the offer, then keep it as a crash point. Returns
    the offer. Both files are full copies, so the restore point is safe
    after the first write; the crash point, written last, exists only once
    the offer is published (see the module docstring)."""
    detected_at = time.time() if now is None else now
    point = store.frozen(snapshot, reason, detected_at)
    entry_id = store.new_id(snapshot["saved_at"])
    offer = {**point, "id": entry_id}
    store.write_pending(offer)
    store.write_crash(entry_id, point)
    logger.info(
        "Workspace restore offer frozen (%s, %d sessions, crash point %s)",
        reason,
        len(snapshot["sessions"]),
        entry_id,
    )
    return offer


def freeze_pending(now: float | None = None) -> bool:
    """Set the restore offer aside before anything overwrites ``latest.json``.

    Keeps an existing offer (Merlin restarted again before the user acted).
    Otherwise freezes ``latest.json`` as a ``startup`` crash point when
    sessions it holds are missing from tmux, counting the terminal's fresh
    ``merlin-dev`` placeholder as missing (``restore.missing_sessions``).
    Returns True when an offer is pending.
    """
    if store.archive_pending() is not None:  # kept, and its archive completed
        return True
    latest = store.read_snapshot(store.latest_path())
    if latest is None or not latest["sessions"]:
        return False
    if not rs.missing_sessions(latest, rs.live_sessions(), rs.live_job_runs()):
        return False
    freeze(latest, "startup", now)
    return True


def record_history(snapshot: dict) -> None:
    """Copy a snapshot the sweep just wrote into ``history/`` and thin it.
    Never raises: the history is a convenience, ``latest.json`` is the
    restore point, and a failure here must not make the sweep rewrite it
    every interval."""
    try:
        store.write_history(snapshot)
        store.thin()
    except Exception:
        logger.warning("Workspace history: write or thinning failed", exc_info=True)


@dataclass
class Observation:
    """One read of tmux: its status (``snap.OK``, ``snap.NO_SERVER`` or
    ``snap.FAILED``) and, when ok, the snapshot and what ``missing_sessions``
    needs, all from the same rows."""

    status: str
    snapshot: dict | None = None
    live: dict[str, bool] | None = None
    job_runs: set[str] = field(default_factory=set)


def observe() -> Observation:
    rows, status = snap.read_tmux_status()
    if rows is None:
        return Observation(status)
    return Observation(
        snap.OK,
        snap.build_snapshot(*rows),
        rs.live_sessions_of(rows),
        rs.job_runs_of(rows),
    )


@dataclass
class SweepState:
    """What one Merlin process carries from sweep to sweep."""

    last: dict | None = None  # the snapshot latest.json holds
    no_server: int = 0  # consecutive sweeps that found no server
    # Identities (store.identity_of) already dealt with: frozen, found frozen
    # on disk, or found with nothing missing. Never looked at again.
    handled: set[tuple] = field(default_factory=set)


def detect(
    state: SweepState,
    reason: str,
    live: dict[str, bool] | None,
    job_runs: set[str],
    now: float | None = None,
) -> dict | None:
    """A rule fired for the server ``state.last`` was taken from: freeze it as
    a crash point and make it the offer, once per server. Returns the offer,
    or None when there was nothing to freeze."""
    disk = state.last
    if disk is None or not disk["sessions"]:
        return None
    identity = store.identity_of(disk)
    if identity in state.handled:
        return None
    if identity in store.frozen_identities():
        state.handled.add(identity)  # frozen before this process started
        return None
    missing = rs.missing_sessions(disk, live, job_runs)
    if not missing:
        state.handled.add(identity)  # rebuilt already, or nothing was lost
        return None
    offer = freeze(disk, reason, now)
    state.handled.add(identity)
    windows, agents = snap.counts(missing)
    logger.warning(
        "tmux server died (%s): %d sessions, %d windows, %d agents lost, offer %s",
        reason,
        len(missing),
        windows,
        agents,
        offer["id"],
    )
    if reason in RUNTIME_REASONS:
        notify_crash(offer, missing)
    return offer


def crash_payload(offer: dict, missing: list[dict], icon: str = "") -> dict:
    """The push for a runtime crash point: what was lost and when, one tag
    for every crash (a second crash replaces the first notification), the
    terminal as the page to open, no window target."""
    _, agents = snap.counts(missing)
    crash = store.crash_of(offer) or {}
    when = time.strftime("%H:%M", time.localtime(crash.get("detected_at", time.time())))
    return {
        "title": "tmux stopped",
        "body": (
            f"{len(missing)} session{'s' if len(missing) != 1 else ''}, "
            f"{agents} agent{'s' if agents != 1 else ''} lost at {when}. "
            "Open Merlin to restore them."
        ),
        "tag": CRASH_PUSH_TAG,
        "url": "/terminal",
        "sid": "",
        "state": "crash",
        "icon": icon,
    }


def notify_crash(offer: dict, missing: list[dict]) -> None:
    """Push the crash to every subscribed device, off the event loop (the
    sweep runs in a thread). Never raises and never delays the freeze, which
    is done by now: a push failure is logged and that is all. Nothing happens
    when notifications are not configured (no subscription: no send, and no
    key pair is generated)."""
    try:
        from notifications.push import current_icon
        from notifications.routes import get_sender

        sender = get_sender()
        if not sender.store.all():
            return
        payload = crash_payload(offer, missing, current_icon())
        result = sender.send_all_sync(payload, ttl=CRASH_PUSH_TTL)
        logger.info(
            "Workspace crash pushed: %d sent, %d failed, %d removed",
            result.sent,
            result.failed,
            result.removed,
        )
    except Exception:
        logger.warning("Workspace crash push failed", exc_info=True)


def sweep_once(state: SweepState, now: float | None = None) -> SweepState:
    """One sweep: finish an interrupted freeze, read tmux, detect a dead
    server, then write when the workspace changed. Returns ``state``,
    updated in place."""
    store.archive_pending()  # raises before any write when it cannot
    seen = observe()
    if seen.status == snap.FAILED:
        return state  # a flaky read: not a dead server, not a live one either
    if seen.status == snap.NO_SERVER:
        state.no_server += 1
        if state.no_server >= NO_SERVER_SWEEPS:
            detect(state, "server-gone", None, set(), now)
        return state
    state.no_server = 0
    current = seen.snapshot
    assert current is not None
    if state.last is not None:
        before, after = store.server_of(state.last), store.server_of(current)
        if before is not None and after is not None and before != after:
            detect(state, "server-changed", seen.live, seen.job_runs, now)
    if not current["sessions"] or snap.same_content(current, state.last):
        return state
    store.write_snapshot(store.latest_path(), current)
    record_history(current)
    state.last = current
    return state


def pending_info() -> dict:
    """What the banner shows: the sessions a restore would bring back now.

    Recomputed against live tmux on every call. When nothing is missing any
    more (the user rebuilt it by hand), the offer is dropped.
    """
    pending = store.read_pending()
    if pending is None:
        return {"pending": False}
    missing = rs.missing_sessions(pending, rs.live_sessions(), rs.live_job_runs())
    if not missing:
        store.delete_pending(pending.get("id"))  # this offer, not a newer one
        return {"pending": False}
    windows, agents = snap.counts(missing)
    crash = store.crash_of(pending) or {}
    return {
        "pending": True,
        "saved_at": pending.get("saved_at"),
        "id": pending.get("id"),
        "reason": crash.get("reason"),
        "crashed_at": crash.get("detected_at"),
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
    """Run the restore of the pending offer, then drop the offer, unless a
    session failed: the offer stays for what is still missing, which
    ``pending_info`` keeps recomputing. A double click, or a CLI restore
    running at the same time, makes the engine raise ``RestoreBusy``: the
    second one does nothing."""
    pending = store.read_pending()
    if pending is None:
        return {"restored": [], "skipped": [], "failed": [], "agents": 0}
    result = rs.restore(pending)
    if not result["failed"]:
        # The offer restored, not one the sweep published meanwhile.
        store.delete_pending(pending.get("id"))
    return result


def dismiss_pending(examined: object = store.ANY_OFFER) -> None:
    """Drop the offer: the one the user saw when it is named, else any."""
    store.delete_pending(examined)


async def run(stop: asyncio.Event, interval: float = SWEEP_INTERVAL) -> None:
    """Freeze the offer, then sweep until ``stop`` is set. Never raises.

    No sweep writes until the freeze has succeeded: a failed freeze (a full
    disk, say) is retried every interval, and ``latest.json``, the only copy of
    the restore point until then, is left alone.
    """
    frozen = False
    state = SweepState()
    logger.info("Workspace snapshot sweep started (every %.0fs)", interval)
    while not stop.is_set():
        if not frozen:
            try:
                await asyncio.to_thread(freeze_pending)
                frozen = True
                state.last = store.read_snapshot(store.latest_path())
            except Exception:
                logger.warning(
                    "Workspace restore: freezing the offer failed", exc_info=True
                )
        if frozen:
            try:
                await asyncio.to_thread(sweep_once, state)
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
