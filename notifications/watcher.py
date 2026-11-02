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

An event carries the facts and the finished text. The facts: how long the
window was ``busy`` before the transition (``busy_seconds``, counted from the
sweep that last saw it enter ``busy``, unknown when Merlin never saw that),
and a ``snippet`` of the agent's last lines, read from the window's pane at
the transition. The capture runs off the event loop, only for windows that
produced an event, and never on the sweep's clock: ``tick`` reserves each
transition in order and starts its capture in an owned task, the next sweep
runs on schedule, and the event is published once its capture completes,
in transition order and with the transition's timestamp. A capture that
fails, times out or yields nothing gives an event without a snippet, never
a missing event. The text: ``title`` and ``body``, composed once here by
``content.compose`` and shown verbatim by the page and the service worker.
Fields are only ever added: the hub forwards these events.

Cursor semantics. A cursor is ``<epoch>:<seq>``. ``epoch`` is minted once per
process, ``seq`` counts events in that process. A page presenting a cursor
from another epoch has survived a Merlin restart: it gets every event of the
new process still in the ring (none of which it can have consumed) and a fresh
cursor. Whenever events the page has not consumed were evicted from the ring,
in either epoch, the response carries their number, so nothing is dropped
silently. A page with no cursor gets no events and the current cursor, so a
reload never replays.

Publication and reads share one lock: the poll handler runs in a worker thread
while the watcher publishes on the event loop, and a cursor must never move
past an event that is not in the ring yet.

A job run's window (``job/runs.py``) carries ``@job_report``, the job's
report mode, ``@job_exit`` once the run's exit code is known and
``@job_finished`` once the supervisor closed the run. The watcher keeps one
track per run (the sid is the run's) and decides the run's own finish
exactly once, by the report mode: ``always`` notifies every finish,
``silent`` only a failure (and every ``ask``), ``off`` nothing. The finish
is recognized from whichever comes first: a ``done`` with the exit code
known, or the run fields confirming completion whatever the state (a
busy-then-idle finish, a ``done`` the sweep missed, an exit stamped before
``done``). A ``done`` seen before its exit code is held back and decided
once the code is there, as a ``done`` with the busy span of the sweep that
saw it (or, under ``always``, as a finish of unknown result after
``DEFER_SWEEPS`` sweeps, corrected by one failure event if a non-zero code
arrives later; under ``silent`` and ``off`` the decision simply waits for
the code). A silenced finish produces no event on any channel. Once the
run's ``done`` or finish was seen, the window is the human's, even while
the finish still waits for its code: a ``busy`` starts their turn, and the
``done`` that ends it, or any ``ask``, notifies like any other window,
whatever the mode; a trailing ``done`` of the run itself (stamped after the
supervisor's finish) notifies nothing. On the watcher's first sweep only
(Merlin restarted), a finished window seen idle or busy is left alone: the
previous process decided it. Later, a window first seen with its finish
stamped is a run that began and ended between two sweeps, decided by the
mode.

Only windows carrying a stable ``@agent_sid`` are tracked. The identity hook
mints the sid at SessionStart, in the same hook group as the state hook, so a
state without a sid is a transient race between the two (SessionStart sets
idle, which never notifies anyway). Tracking such a row under a provisional
key would emit the same transition a second time once the sid appears.

A missing tmux server is an ordinary state: the sweep reports it, the watcher
remembers it for the popover, keeps its last known states so a transient
failure never re-fires, and retries on the next tick. Nothing here raises into
the server's task list.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .content import clean_snippet, compose

if TYPE_CHECKING:
    from board.sweep import Window

logger = logging.getLogger("merlin.notifications")

ATTENTION_STATES = frozenset({"done", "ask"})
SWEEP_INTERVAL = 2.0
# How many sweeps a job window's done may wait for its exit code before it
# is decided without one under ``always`` (the supervisor stamps it within
# one poll; a dead one is adopted at the next scheduler tick).
DEFER_SWEEPS = 10
RING_SIZE = 200
# How long a stopping watcher waits for the captures in flight (each is
# bounded by the capture's own timeout) before cancelling them.
CAPTURE_SETTLE = 4.0


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
    machine: str = ""
    busy_seconds: int | None = None
    snippet: str = ""
    title: str = ""
    body: str = ""
    # Someone was looking at the window (a visible, focused page displaying
    # it) when the event was produced: no channel shows it. Decided here,
    # once, so the push sender and every page obey the same answer.
    quiet: bool = False
    # A job run's own finish: the job and the run's exit code. Empty and
    # None on every other event, a human's later turn in the window included.
    job_id: str = ""
    exit_code: int | None = None

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


@dataclass
class _Reserved:
    """A transition seen by a sweep, waiting for its pane capture before it
    is published. ``snippet`` is None until the capture completed."""

    window: Window
    busy_seconds: int | None
    ts: str
    snippet: str | None = None
    job_id: str = ""
    exit_code: int | None = None


@dataclass
class Transition:
    """One attention transition a sweep produced, with what the job rule
    decided: ``job_id`` and ``exit_code`` set for a run's own finish."""

    window: Window
    busy_seconds: int | None
    job_id: str = ""
    exit_code: int | None = None


@dataclass
class _JobTrack:
    """What the watcher knows of one job run's window (one per sid)."""

    handled: bool = False  # the run's own finish was decided (either way)
    unknown: bool = False  # decided under always without an exit code
    human_turn: bool = False  # a busy seen after the finish: the human's turn
    deferred: Window | None = None  # the done seen before its exit code
    deferred_busy: int | None = None
    deferred_sweeps: int = 0


def _capture_window(w: Window) -> str | None:
    from board.sweep import capture_pane

    return capture_pane(w.session, w.window_id)


def _no_capture(_w: Window) -> str | None:
    return None


class Watcher:
    """Diff successive sweeps into attention events. Pure about tmux: it only
    ever reads sweeps and panes through the callables it was given.

    With no ``sweep`` the watcher reads the real tmux server and captures
    real panes. A scripted sweep has no panes behind it, so it captures
    nothing unless a ``capture`` is given too. ``machine`` is the environment
    name in every title (``resolve_machine_name`` when not given)."""

    def __init__(
        self,
        sweep: Callable[[], list[Window] | None] | None = None,
        *,
        capture: Callable[[Window], str | None] | None = None,
        machine: str | None = None,
        interval: float = SWEEP_INTERVAL,
        ring_size: int = RING_SIZE,
        clock: Callable[[], float] = time.time,
        looking_at: Callable[[str], bool] | None = None,
    ) -> None:
        if sweep is None:
            from board.sweep import run_sweep_checked

            sweep = run_sweep_checked
            capture = capture or _capture_window
        self._sweep = sweep
        self._capture = capture or _no_capture
        self._machine = machine
        # Whether a page is looking at a window right now, supplied by the
        # terminal (it knows its clients). Stamps ``Event.quiet``.
        self.looking_at: Callable[[str], bool] = looking_at or (lambda _t: False)
        self.interval = interval
        self.capture_settle = CAPTURE_SETTLE
        self._clock = clock
        self.epoch = secrets.token_hex(4)
        self._seq = 0
        self._ring: deque[Event] = deque(maxlen=ring_size)
        self._last: dict[str, str] = {}
        # sid -> clock time of the sweep that last saw the window enter busy.
        self._busy_since: dict[str, float] = {}
        # sid -> the track of a job run's window.
        self._jobs: dict[str, _JobTrack] = {}
        # Whether a sweep was folded in yet: the first one is a replay.
        self._started = False
        # Transitions in order, each waiting for its capture (the task path).
        self._reserved: deque[_Reserved] = deque()
        self._captures: set[asyncio.Task] = set()
        self._lock = threading.Lock()
        self._listeners: list[Callable[[Event], None]] = []
        self.tmux_available: bool | None = None  # None until the first sweep
        self.swept_once = False

    @property
    def machine(self) -> str:
        """The environment name, resolved once on first use (after
        ``config.env`` is loaded, not at import)."""
        if self._machine is None:
            from merlin_ext import resolve_machine_name

            self._machine = resolve_machine_name()
        return self._machine

    # -- events ----------------------------------------------------------

    @property
    def cursor(self) -> str:
        with self._lock:
            return self._cursor()

    def _cursor(self) -> str:
        return f"{self.epoch}:{self._seq}"

    def add_listener(self, fn: Callable[[Event], None]) -> None:
        """Receive every event as it is produced. A listener that raises is
        logged and never stops the watcher or the other listeners."""
        self._listeners.append(fn)

    def observe(self, windows: list[Window] | None) -> list[Event]:
        """Fold one sweep into the state and return the events it produced,
        capturing the panes synchronously (``tick`` is the off-loop path).

        ``None`` means the sweep could not run (no tmux server, or tmux
        failed): the previous states are kept so a transient failure never
        re-fires a notification when the server comes back with the same
        windows.
        """
        pending = self._transitions(windows)
        return [
            self._emit(
                t.window,
                t.busy_seconds,
                self._snippet_of(t.window),
                self._stamp(),
                job_id=t.job_id,
                exit_code=t.exit_code,
            )
            for t in pending
        ]

    def _stamp(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc).isoformat()

    def _transitions(self, windows: list[Window] | None) -> list[Transition]:
        """Diff one sweep against the last: the windows that transitioned
        into ``done`` or ``ask``, each with how long it was busy before, or
        None when the watcher never saw it enter ``busy``, after the job rule
        (a job run's own finish is decided by its report mode and exit code,
        and held back while the code is not there yet). Updates the state,
        emits nothing."""
        self.swept_once = True
        if windows is None:
            self.tmux_available = False
            return []
        self.tmux_available = True

        current: dict[str, str] = {}
        seen: dict[str, Window] = {}
        for w in windows:
            if not w.state or not w.sid:
                continue
            current[w.sid] = w.state
            seen[w.sid] = w

        now = self._clock()
        pending: list[Transition] = []
        for key, state in current.items():
            prev = self._last.get(key)
            w = seen[key]
            if state == "busy":
                # Entering busy from a state this watcher saw. A window first
                # seen busy went busy before Merlin looked: unknown start.
                if prev is not None and prev != "busy":
                    self._busy_since[key] = now
            if w.is_job:
                pending.extend(
                    self._job_window(key, w, prev, now, replay=not self._started)
                )
                continue
            if state == "busy" or state not in ATTENTION_STATES or prev == state:
                continue
            pending.append(Transition(w, self._busy_span(key, now)))
        self._busy_since = {k: v for k, v in self._busy_since.items() if k in current}
        self._jobs = {k: v for k, v in self._jobs.items() if k in current}
        self._last = current
        self._started = True
        return pending

    def _busy_span(self, key: str, now: float) -> int | None:
        since = self._busy_since.pop(key, None)
        return int(round(max(0.0, now - since))) if since is not None else None

    def _job_window(
        self, key: str, w: Window, prev: str | None, now: float, replay: bool
    ) -> list[Transition]:
        """One sweep of a job run's window: the run's own finish, decided
        once from the state or the run fields, then the human's turns (the
        transitions after the run's done or finish was seen)."""
        t = self._jobs.get(key)
        if t is None:
            t = self._jobs[key] = _JobTrack()
            if replay and (w.job_exit or w.job_finished) and w.state != "done":
                # The watcher's first sweep (Merlin restarted): a window
                # finished long ago, idle or busy, was decided by the
                # previous process. Nothing to notify now, and the busy it
                # may show is not a human turn: the run's trailing done
                # stays silent until a turn starts from a state seen here.
                t.handled = True
                return []
        state = w.state
        out: list[Transition] = []
        if not t.handled:
            # Was the run's done (or its finish) seen at an earlier sweep?
            after_done = t.deferred is not None
            out.extend(self._run_finish(key, t, w, prev, now))
            if not t.handled and t.deferred is None:
                # Still the run's: its own question is told by the mode.
                if state == "ask" and prev != "ask" and w.job_report != "off":
                    out.append(
                        Transition(w, self._busy_span(key, now), job_id=w.job_id)
                    )
                return out
            if not after_done:
                # This sweep showed the run's done or finish for the first
                # time: nothing in it is the human's yet (a busy the finish
                # was stamped over is the run's own).
                return out
        elif t.unknown and w.job_exit:
            # The late result of a finish told without one: one failure
            # event, under the mode (always, the only mode that tells an
            # unknown result).
            t.unknown = False
            code = int(w.job_exit) if w.job_exit.lstrip("-").isdigit() else 0
            if code != 0:
                late = self._job_finish(replace(w, state="done"), None)
                if late is not None:
                    out.append(late)
        # After the run's done: the window is the human's. Their turns are
        # tracked even while the run's own finish still waits for its
        # exit code.
        if state == "busy" and prev != "busy":
            t.human_turn = True
        elif state == "done" and prev != "done":
            if t.human_turn:
                t.human_turn = False
                out.append(Transition(w, self._busy_span(key, now)))
            # Else the run's own trailing done (run.sh stamps it after the
            # supervisor's finish): already decided, nothing.
        elif state == "ask" and prev != "ask":
            out.append(Transition(w, self._busy_span(key, now)))
        return out

    def _run_finish(
        self, key: str, t: _JobTrack, w: Window, prev: str | None, now: float
    ) -> list[Transition]:
        """The run's own finish, if this sweep shows it: a done with the
        exit known, the run fields confirming completion whatever the
        state, or the exit a held-back done was waiting for."""
        state = w.state
        if t.deferred is not None:
            t.deferred_sweeps += 1
            if w.job_exit or w.job_finished:
                decided = self._decide(t, t.deferred, t.deferred_busy, w)
                return [decided] if decided is not None else []
            if t.deferred_sweeps >= DEFER_SWEEPS and w.job_report == "always":
                # Nothing came: tell of the finish without its result, and
                # keep the door open for one failure event.
                t.handled = t.unknown = True
                told, t.deferred = t.deferred, None
                return [Transition(told, t.deferred_busy, job_id=w.job_id)]
            return []
        if (w.job_exit or w.job_finished) and (state != "done" or prev == "done"):
            # The run fields confirm the finish, whatever the state: a
            # busy-then-idle finish, a done the sweep missed, an exit
            # stamped before done.
            done = replace(w, state="done")
            decided = self._decide(t, done, self._busy_span(key, now), w)
            return [decided] if decided is not None else []
        if state == "done" and prev != "done":
            busy = self._busy_span(key, now)
            if w.job_exit or w.job_finished:
                decided = self._decide(t, w, busy, w)
                return [decided] if decided is not None else []
            # The agent's done reached the window before the supervisor's
            # exit: decided at a later sweep, as this done, never as a
            # plain one.
            t.deferred, t.deferred_busy, t.deferred_sweeps = w, busy, 0
        return []

    def _decide(
        self, t: _JobTrack, done: Window, busy: int | None, fields: Window
    ) -> Transition | None:
        """The run's own finish, once: ``done`` is the window as the finish
        is told (state done), ``fields`` the window read now, whose exit
        code and report mode decide. None is silence."""
        exit_code = fields.job_exit
        if not exit_code and fields.job_report != "always":
            # Confirmed finished, the code not there yet (the finish stamp
            # came first): silent and off have nothing to decide without
            # it. Hold the done until the code arrives.
            if t.deferred is None:
                t.deferred, t.deferred_busy, t.deferred_sweeps = done, busy, 0
            return None
        t.handled = True
        t.deferred = None
        if not exit_code:
            # Told as a finish of unknown result, corrected by one failure
            # event if a non-zero code arrives.
            t.unknown = True
        return self._job_finish(replace(done, job_exit=exit_code), busy)

    @staticmethod
    def _job_finish(w: Window, busy: int | None) -> Transition | None:
        """The report mode applied to a run's own finish: ``always`` notifies
        it, ``silent`` only when it failed, ``off`` never. None is silence."""
        code = int(w.job_exit) if w.job_exit.lstrip("-").isdigit() else None
        failed = code is not None and code != 0
        if w.job_report == "off":
            return None
        if w.job_report == "silent" and not failed:
            return None
        return Transition(w, busy, job_id=w.job_id, exit_code=code)

    def _snippet_of(self, w: Window) -> str:
        """The cleaned snippet of a window's pane. Never raises: a capture
        that fails is an empty snippet, and the event still goes out."""
        try:
            return clean_snippet(self._capture(w))
        except Exception:
            logger.exception("Pane capture failed for %s:%s", w.session, w.window_id)
            return ""

    def _emit(
        self,
        w: Window,
        busy_seconds: int | None,
        snippet: str,
        ts: str,
        *,
        job_id: str = "",
        exit_code: int | None = None,
    ) -> Event:
        machine = self.machine
        title, body = compose(
            state=w.state,
            window_name=w.name,
            session=w.session,
            machine=machine,
            busy_seconds=busy_seconds,
            snippet=snippet,
            exit_code=exit_code,
        )
        try:
            quiet = bool(self.looking_at(f"{w.session}:{w.window_id}"))
        except Exception:
            logger.exception("looking_at failed, event not quiet")
            quiet = False
        # Seq and ring move together under the lock, so a concurrent read
        # never sees a cursor ahead of the events it can return.
        with self._lock:
            self._seq += 1
            ev = Event(
                seq=self._seq,
                sid=w.sid,
                session=w.session,
                window_id=w.window_id,
                window_name=w.name,
                state=w.state,
                project=_project_of(w.cwd),
                ts=ts,
                machine=machine,
                busy_seconds=busy_seconds,
                snippet=snippet,
                title=title,
                body=body,
                quiet=quiet,
                job_id=job_id,
                exit_code=exit_code,
            )
            self._ring.append(ev)
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                logger.exception("Notification listener failed")
        return ev

    def events_since(self, cursor: str | None) -> tuple[list[Event], str, int]:
        """Events after ``cursor``, the new cursor, and how many unconsumed
        events were evicted from the ring before they could be read (0
        normally).

        No cursor: nothing, just the current position. Another epoch: every
        event this process still holds, with the count of those it no longer
        holds. Same epoch: the events after ``seq``, with the count of those
        between ``seq`` and the oldest kept one. All three values come from
        one snapshot taken under the publication lock.
        """
        with self._lock:
            now = self._cursor()
            if not cursor:
                return [], now, 0
            epoch, _, seq_s = cursor.partition(":")
            since = int(seq_s) if epoch == self.epoch and seq_s.isdigit() else 0
            if since >= self._seq:
                return [], now, 0
            oldest = self._ring[0].seq if self._ring else self._seq + 1
            dropped = max(0, oldest - since - 1)
            return [e for e in self._ring if e.seq > since], now, dropped

    # -- the background task ----------------------------------------------

    async def tick(self) -> int:
        """One sweep off the event loop, folded in. Every transition is
        reserved in order with its timestamp and its pane capture starts in
        an owned task: the sweep never waits for a capture, so a stalled pane
        cannot delay the next sweep or hide a later transition. Returns the
        number of transitions reserved. Never raises."""
        try:
            windows = await asyncio.to_thread(self._sweep)
        except Exception:
            logger.exception("Attention sweep failed")
            windows = None
        try:
            pending = self._transitions(windows)
        except Exception:
            logger.exception("Attention diff failed")
            return 0
        for t in pending:
            item = _Reserved(
                t.window,
                t.busy_seconds,
                self._stamp(),
                job_id=t.job_id,
                exit_code=t.exit_code,
            )
            self._reserved.append(item)
            task = asyncio.create_task(self._enrich(item), name="notifications-capture")
            self._captures.add(task)
            task.add_done_callback(self._captures.discard)
        return len(pending)

    async def _enrich(self, item: _Reserved) -> None:
        """Capture the pane of one reserved transition off the loop, then
        publish every reserved transition whose capture is complete, oldest
        first. Only shutdown cancels this: the reservation is then withdrawn
        so the ones behind it are never wedged."""
        try:
            snippet = await asyncio.to_thread(self._snippet_of, item.window)
        except asyncio.CancelledError:
            # Withdraw this reservation and let the ones behind it that
            # already have their snippet go out, so nothing is left wedged.
            with contextlib.suppress(ValueError):
                self._reserved.remove(item)
            self._publish_ready()
            raise
        except Exception:
            logger.exception("Pane capture failed")
            snippet = ""
        item.snippet = snippet
        self._publish_ready()

    def _publish_ready(self) -> None:
        """Publish the head of the reservation queue while its capture is
        done, so events go out in transition order."""
        while self._reserved:
            snippet = self._reserved[0].snippet
            if snippet is None:
                break
            item = self._reserved.popleft()
            self._emit(
                item.window,
                item.busy_seconds,
                snippet,
                item.ts,
                job_id=item.job_id,
                exit_code=item.exit_code,
            )

    async def settle(self, timeout: float | None = None) -> None:
        """Wait for the captures in flight, up to ``timeout``, so that their
        events are published. Shutdown and tests use it."""
        tasks = [t for t in self._captures if not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def run(self, stop: asyncio.Event) -> None:
        """Sweep every ``interval`` seconds until ``stop`` is set, then let
        the captures in flight publish (bounded by ``capture_settle``),
        cancel any that overrun and wait for their cleanup, so the watcher
        returns with no reservation and no task left behind."""
        logger.info("Attention watcher started (every %.1fs)", self.interval)
        try:
            while not stop.is_set():
                await self.tick()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval)
                except TimeoutError:
                    pass
        finally:
            try:
                await self.settle(self.capture_settle)
            finally:
                await self._cancel_captures()
                logger.info("Attention watcher stopped")

    async def _cancel_captures(self) -> None:
        """Cancel the captures still running and wait for each one's cleanup
        (the reservation withdrawn, the followers published), then make sure
        no reservation survives, so a later ``run`` starts clean."""
        pending = [t for t in self._captures if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._reserved:
            logger.warning(
                "Attention watcher dropped %d reservation(s) at stop",
                len(self._reserved),
            )
            self._reserved.clear()


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
    """Stop the watcher task and wait for it, cancelling if it overruns.

    A cancellation of this coroutine itself cancels the watcher task and is
    then re-raised, so a caller's cancellation is never swallowed."""
    global _task, _stop
    task, event = _task, _stop
    _task = _stop = None
    if task is None or event is None:
        return
    event.set()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except TimeoutError:
        logger.warning("Attention watcher did not stop in %.0fs, cancelling", timeout)
        task.cancel()
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        raise
    except Exception:
        logger.exception("Attention watcher ended with an error")
