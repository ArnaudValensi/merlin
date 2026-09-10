"""Attention watcher: turn ``@agent_state`` transitions into attention events.

A background task runs the same ``tmux list-windows -a -F`` sweep the sessions
panel uses (``board/sweep.py``) every couple of seconds and diffs it, keyed by
the stable ``@agent_sid``, against the previous sweep. A window that enters
``done`` or ``ask`` from any other state, or that is seen for the first time in
one of those states, produces exactly one attention event. Transitions into
``busy`` or idle produce nothing. The same state observed twice in a row is not
a transition.

The watcher knows nothing about delivery. Events sit in a bounded in-memory
ring that pages read through a cursor (``events_since``), and listeners
registered with ``add_listener`` receive each event as it is produced. The hub
epic forwards these events from a node without touching tmux here.

Cursor semantics. A cursor is ``<epoch>:<seq>``. ``epoch`` is minted once per
process, ``seq`` counts events in that process. A page presenting a cursor
from another epoch has survived a Merlin restart: it gets every event of the
new process (none of which it can have consumed) and a fresh cursor. A page
whose ``seq`` fell behind the ring gets what is left plus the number of events
that were lost, so nothing is dropped silently. A page with no cursor gets no
events and the current cursor, so a reload never replays.

A missing tmux server is an ordinary state: the sweep reports it, the watcher
remembers it for the popover, keeps its last known states so a transient
failure never re-fires, and retries on the next tick. Nothing here raises into
the server's task list.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from board.sweep import Window

logger = logging.getLogger("merlin.notifications")

ATTENTION_STATES = frozenset({"done", "ask"})
SWEEP_INTERVAL = 2.0
RING_SIZE = 200


@dataclass(frozen=True)
class Event:
    """One attention event: a window flipped into ``done`` or ``ask``."""

    seq: int
    sid: str
    session: str
    window_id: str
    window_name: str
    state: str
    project: str
    ts: str

    @property
    def target(self) -> str:
        """The tmux target of the window, as the terminal's switch takes it."""
        return f"{self.session}:{self.window_id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["target"] = self.target
        return d


def _project_of(cwd: str) -> str:
    return os.path.basename(cwd.rstrip("/")) if cwd else ""


def _key_of(w: Window) -> str:
    """The identity a window is tracked by: its stable sid, or its window id
    for an agent window that carries a state but no sid (started before the
    identity hook was installed)."""
    return w.sid or f"win:{w.window_id}"


class Watcher:
    """Diff successive sweeps into attention events. Pure about tmux: it only
    ever reads sweeps, through the callable it was given."""

    def __init__(
        self,
        sweep: Callable[[], list[Window] | None] | None = None,
        *,
        interval: float = SWEEP_INTERVAL,
        ring_size: int = RING_SIZE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if sweep is None:
            from board.sweep import run_sweep_checked

            sweep = run_sweep_checked
        self._sweep = sweep
        self.interval = interval
        self._clock = clock
        self.epoch = secrets.token_hex(4)
        self._seq = 0
        self._ring: deque[Event] = deque(maxlen=ring_size)
        self._last: dict[str, str] = {}
        self._listeners: list[Callable[[Event], None]] = []
        self.tmux_available: bool | None = None  # None until the first sweep
        self.swept_once = False

    # -- events ----------------------------------------------------------

    @property
    def cursor(self) -> str:
        return f"{self.epoch}:{self._seq}"

    def add_listener(self, fn: Callable[[Event], None]) -> None:
        """Receive every event as it is produced. A listener that raises is
        logged and never stops the watcher or the other listeners."""
        self._listeners.append(fn)

    def observe(self, windows: list[Window] | None) -> list[Event]:
        """Fold one sweep into the state and return the events it produced.

        ``None`` means the sweep could not run (no tmux server, or tmux
        failed): the previous states are kept so a transient failure never
        re-fires a notification when the server comes back with the same
        windows.
        """
        self.swept_once = True
        if windows is None:
            self.tmux_available = False
            return []
        self.tmux_available = True

        current: dict[str, str] = {}
        seen: dict[str, Window] = {}
        for w in windows:
            if not w.state:
                continue
            key = _key_of(w)
            current[key] = w.state
            seen[key] = w

        events: list[Event] = []
        for key, state in current.items():
            if state not in ATTENTION_STATES:
                continue
            if self._last.get(key) == state:
                continue
            events.append(self._emit(seen[key], key))
        self._last = current
        return events

    def _emit(self, w: Window, key: str) -> Event:
        self._seq += 1
        ev = Event(
            seq=self._seq,
            sid=key,
            session=w.session,
            window_id=w.window_id,
            window_name=w.name,
            state=w.state,
            project=_project_of(w.cwd),
            ts=datetime.fromtimestamp(self._clock(), tz=timezone.utc).isoformat(),
        )
        self._ring.append(ev)
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                logger.exception("Notification listener failed")
        return ev

    def events_since(self, cursor: str | None) -> tuple[list[Event], str, int]:
        """Events after ``cursor``, the new cursor, and how many events between
        the cursor and the oldest kept one were lost to the ring (0 normally).

        No cursor: nothing, just the current position. Another epoch: every
        event this process holds. Same epoch: the events after ``seq``.
        """
        if not cursor:
            return [], self.cursor, 0
        epoch, _, seq_s = cursor.partition(":")
        if epoch != self.epoch or not seq_s.isdigit():
            return list(self._ring), self.cursor, 0
        since = int(seq_s)
        if since >= self._seq:
            return [], self.cursor, 0
        oldest = self._ring[0].seq if self._ring else self._seq + 1
        dropped = max(0, oldest - since - 1)
        return [e for e in self._ring if e.seq > since], self.cursor, dropped

    # -- the background task ----------------------------------------------

    async def tick(self) -> list[Event]:
        """One sweep off the event loop, folded in. Never raises."""
        try:
            windows = await asyncio.to_thread(self._sweep)
        except Exception:
            logger.exception("Attention sweep failed")
            windows = None
        try:
            return self.observe(windows)
        except Exception:
            logger.exception("Attention diff failed")
            return []

    async def run(self, stop: asyncio.Event) -> None:
        """Sweep every ``interval`` seconds until ``stop`` is set."""
        logger.info("Attention watcher started (every %.1fs)", self.interval)
        try:
            while not stop.is_set():
                await self.tick()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval)
                except TimeoutError:
                    pass
        finally:
            logger.info("Attention watcher stopped")


watcher = Watcher()

_task: asyncio.Task | None = None
_stop: asyncio.Event | None = None


def start() -> asyncio.Task:
    """Start the singleton watcher as a background task (once)."""
    global _task, _stop
    if _task is not None and not _task.done():
        return _task
    _stop = asyncio.Event()
    _task = asyncio.create_task(watcher.run(_stop), name="notifications-watcher")
    return _task


async def stop(timeout: float = 6.0) -> None:
    """Stop the watcher task and wait for it, cancelling if it overruns."""
    global _task, _stop
    task, event = _task, _stop
    _task = _stop = None
    if task is None or event is None:
        return
    event.set()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
    except Exception:
        logger.exception("Attention watcher ended with an error")
