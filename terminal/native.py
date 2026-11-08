"""Native terminal: the ``/ws/terminal2`` WebSocket, a tmux control-mode bridge.

The classic terminal (``/ws/terminal``, ``terminal/routes.py``) runs a tmux
client on a PTY and streams the screen tmux draws for it. This bridge attaches
a tmux client in control mode instead (``terminal/control.py``), so nothing is
drawn for it, and mirrors tmux's own grid of the displayed pane to the page:
the rows of its screen and of its history, exactly as tmux holds them, with
each row's wrap flag. The page turns rows into real DOM text in a native
scrolling container and keeps the history itself, so it can scroll and select
natively. Both terminals attach to the same tmux server and can be open at once.

Why the grid and not the raw output: tmux interprets every byte a program
writes, and part of that interpretation (the current rendition, the charsets,
a saved cursor, an unfinished UTF-8 character) is state tmux reports to no
client. A page that re-parsed the raw bytes after attaching to a running pane
could not know it, and would draw the following text differently from tmux.
Rows captured from tmux's grid are what tmux itself shows, whatever happened
before Merlin attached. See the native-terminal journal (2026-11-08) for the
measurements and the comparison.

What the bridge guarantees (pinned by ``tests/unit/test_terminal_native.py``):

- **Exact rows.** Every update is one tmux command line: the pane's state
  (formats) and ``capture-pane -p -e -N -L -F`` of the rows it needs. ``-L``
  numbers each row relative to the screen top and ``history_added`` (read in
  the same line) makes it absolute, so history rows are sent once each, in
  order, even while output keeps scrolling the pane; ``-F`` gives each row's
  wrap flag (``W``). A row before the range (the overlap row) tells whether
  the first new row continues a row the page already has.
- **Pulled, bounded.** Output of the displayed pane only marks it dirty; the
  next update is taken when the page has room in its window of unacknowledged
  bytes, at most every ``UPDATE_INTERVAL``. Pending history stays in tmux, not
  here. Every frame (updates, history ranges, control frames, broadcasts) is
  admitted against the window; a history batch is capped at
  ``HISTORY_BYTES_MAX``; one frame larger than the window (a huge screen) is
  only sent when nothing is outstanding; a page past ``HARD_LIMIT`` is closed
  and reconnects. Nothing is ever paused in tmux: a pane paused by its only
  client stops being read, which would freeze the program writing to it.
- **Input exactly once.** Text goes as ``send-keys -H`` in 1 KiB commands, a
  paste through a private paste buffer with ``paste-buffer -p`` (tmux adds the
  bracketed-paste markers when the program asked for them), a named key as
  ``send-keys``: with ``-K`` when tmux binds it (the prefix, a root binding such
  as ``Escape`` or ``F2``, any key while a key table other than root is active),
  so the binding runs exactly as for a typed key; plainly otherwise, so tmux
  encodes it for the pane's own modes.
- **The running server is checked.** The control connection asks the server it
  attached to for its version and the formats the bridge needs; below 3.8 the
  page gets an ``unsupported`` notice and falls back to the classic terminal.
  The installed binary is only checked when no server runs (it would create
  one). Every probe runs off the event loop and bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import tempfile
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import cast

from fastapi import WebSocket, WebSocketDisconnect

from auth import verify_ws_cookie
from board import sweep as board_sweep
from terminal import routes as classic
from terminal.control import ControlClient, ControlError, hex_keys, quote
from terminal.tmux import (
    parse_session_identity,
    reconnect_argv,
    server_spawn_prefix,
    terminal_process_env,
    tmux_conf_args,
)

logger = logging.getLogger("merlin.terminal")

MIN_TMUX = (3, 8)

# Binary frames to the page: one kind byte, a JSON header, a NUL, the rows
# (``capture-pane -e`` lines, separated by LF).
FRAME_UPDATE = 0x01  # the displayed pane: new history rows and the screen
FRAME_HISTORY = 0x03  # older history rows, asked for by the page

# The flow-control window, in bytes the page has not acknowledged yet.
HIGH_WATER = 1024 * 1024
LOW_WATER = 256 * 1024
# Past this the page is not reading at all: close it, it reconnects.
HARD_LIMIT = 4 * HIGH_WATER
HISTORY_BYTES_MAX = 256 * 1024
UPDATE_INTERVAL = 0.016
CLIPBOARD_MAX = 1024 * 1024
SEND_TIMEOUT = 10.0
PROBE_TIMEOUT = 5.0
INPUT_CHUNK = 1024
INPUT_COMMANDS_PER_LINE = 8
PASTE_MAX = 1024 * 1024
INITIAL_HISTORY = 1000
# History rows asked for in one capture.
ROWS_PER_CAPTURE = 2000
HISTORY_PAGE_MAX = 2000
CLOSE_UNSUPPORTED = 4426
CLOSE_STALLED = 4408
CLIPBOARD_BUFFER = "merlin-clip"

_PANE_ID = re.compile(r"%\d+")
# A tmux key name: optional modifiers, then a name or one printable character.
_KEY_NAME = re.compile(r"(?:[CMS]-){0,3}(?:[A-Za-z][A-Za-z0-9]{0,15}|[!-~])")
_VERSION = re.compile(r"(\d+)\.(\d+)")
# A ``capture-pane -L -F`` line: relative line number, flags, the row.
_ROW = re.compile(rb"(-?\d+) (\S+) (.*)", re.S)

_PANE_FIELDS = (
    "pane_id",
    "pane_width",
    "pane_height",
    "cursor_x",
    "cursor_y",
    "cursor_flag",
    "cursor_shape",
    "cursor_blinking",
    "alternate_on",
    "mouse_standard_flag",
    "mouse_button_flag",
    "mouse_all_flag",
    "mouse_utf8_flag",
    "mouse_sgr_flag",
    "bracket_paste_flag",
    "history_size",
    "history_added",
    "history_collected",
    "history_generation",
)
_PANE_FMT = "\t".join("#{" + f + "}" for f in _PANE_FIELDS)
# Formats the bridge cannot work without: empty on a server that is too old.
_REQUIRED = ("bracket_paste_flag", "history_added", "history_collected")

# What this client displays: the session frame's fields, then its pane.
_CLIENT_FMT = (
    "#{session_name}\t#{session_id}\t#{session_created}\t#{window_id}\t"
    "#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_width}\t#{pane_height}"
)
_WINDOW_FMT = (
    "#{window_id}\t#{window_index}\t#{window_name}\t#{window_active}\t#{@agent_state}"
)


# -- versions ---------------------------------------------------------------------


def parse_tmux_version(text: str) -> tuple[int, int] | None:
    """``tmux 3.8`` or ``3.8`` -> (3, 8); ``3.7c`` -> (3, 7); ``next-3.9`` ->
    (3, 9). A build without a number (``master``) is newer than any release
    and reads as (99, 0). None for anything else."""
    text = text.strip()
    if text.startswith("tmux "):
        text = text[len("tmux ") :]
    if not text or " " in text:
        return None
    match = _VERSION.search(text)
    if match is None:
        return (99, 0) if text == "master" else None
    return int(match.group(1)), int(match.group(2))


def supported(version: tuple[int, int] | None) -> bool:
    return version is not None and version >= MIN_TMUX


async def run_tmux(args: Sequence[str], timeout: float = PROBE_TIMEOUT) -> str | None:
    """``tmux <args>`` off the event loop, bounded; stdout, or None on any
    failure (missing binary, error, timeout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 1.0)
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace")


async def binary_version() -> str:
    """The installed tmux's answer to ``-V`` (no server needed), or ""."""
    return (await run_tmux(["-V"]) or "").strip()


# -- the grid ---------------------------------------------------------------------


@dataclass(frozen=True)
class PaneModes:
    """A pane's state as tmux reports it."""

    values: dict[str, str]

    @classmethod
    def parse(cls, line: str) -> PaneModes | None:
        parts = line.split("\t")
        if len(parts) != len(_PANE_FIELDS):
            return None
        return cls(dict(zip(_PANE_FIELDS, parts, strict=True)))

    def num(self, name: str, default: int = 0) -> int:
        value = self.values.get(name, "")
        return int(value) if value.lstrip("-").isdigit() else default

    def flag(self, name: str) -> bool:
        return self.values.get(name) == "1"

    def complete(self) -> bool:
        return all(self.values.get(name, "") != "" for name in _REQUIRED)

    @property
    def pane(self) -> str:
        return self.values["pane_id"]

    @property
    def cols(self) -> int:
        return self.num("pane_width", 80)

    @property
    def rows(self) -> int:
        return self.num("pane_height", 24)

    @property
    def added(self) -> int:
        return self.num("history_added")

    def state(self) -> dict:
        """What the page needs besides the rows."""
        return {
            "pane": self.pane,
            "cols": self.cols,
            "rows": self.rows,
            "cursor": {
                "x": min(self.num("cursor_x"), self.cols - 1),
                "y": self.num("cursor_y"),
                "visible": self.flag("cursor_flag"),
                "shape": self.values.get("cursor_shape", "default"),
                "blink": self.flag("cursor_blinking"),
            },
            "alternate": self.flag("alternate_on"),
            "mouse": {
                "standard": self.flag("mouse_standard_flag"),
                "button": self.flag("mouse_button_flag"),
                "all": self.flag("mouse_all_flag"),
                "sgr": self.flag("mouse_sgr_flag"),
                "utf8": self.flag("mouse_utf8_flag"),
            },
            "history": {
                "added": self.added,
                "collected": self.num("history_collected"),
                "generation": self.num("history_generation"),
            },
        }


@dataclass(frozen=True)
class Row:
    """One row of a pane's grid. ``number`` counts from the pane's first row
    ever (history rows below ``history_added``, the screen from it on);
    ``wrapped``: the row continues on the next one."""

    number: int
    wrapped: bool
    data: bytes


def parse_rows(lines: Sequence[bytes], added: int) -> list[Row]:
    """Rows of a ``capture-pane -p -e -N -L -F`` reply. ``added`` is the
    pane's ``history_added`` read in the same command line, which turns the
    relative line numbers into absolute ones."""
    rows = []
    for line in lines:
        m = _ROW.fullmatch(line)
        if m is None:
            continue
        rows.append(Row(added + int(m.group(1)), b"W" in m.group(2), m.group(3)))
    return rows


def capture(target: str, start: int, end: int | str) -> str:
    """The capture command for relative lines ``start``..``end``."""
    return f"capture-pane -p -e -N -L -F -S {start} -E {end} -t {target}"


def _frame(kind: int, header: dict, rows: Sequence[bytes]) -> bytes:
    return (
        bytes([kind])
        + json.dumps(header, separators=(",", ":")).encode()
        + b"\x00"
        + b"\n".join(rows)
    )


def _flags(rows: Sequence[Row]) -> str:
    return "".join("1" if r.wrapped else "0" for r in rows)


# -- the outbox -----------------------------------------------------------------------


@dataclass
class _Frame:
    data: bytes
    text: bool = False
    on_sent: Callable[[], None] | None = None


@dataclass
class _Outbox:
    """Frames waiting for the page, in order, and the flow-control counters.
    Every frame counts with its full length; the page acknowledges the total
    length of the frames it has handled."""

    frames: deque[_Frame] = field(default_factory=deque)
    queued: int = 0
    sent: int = 0
    acked: int = 0
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def unacked(self) -> int:
        return self.sent - self.acked + self.queued

    def push(self, frame: _Frame) -> None:
        self.frames.append(frame)
        self.queued += len(frame.data)
        self.wake.set()


class _PageSocket:
    """What ``routes.broadcast_control`` writes to for a native page: its
    frames join the native outbox (ordered, counted, timed by the one sender)
    instead of a second writer on the socket."""

    def __init__(self, bridge: NativeBridge) -> None:
        self.bridge = bridge

    async def send_text(self, text: str) -> None:
        if not self.bridge.push_control_text(text):
            raise RuntimeError("native page is not reading")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self.bridge.close_socket(code, reason)


# -- the bridge -------------------------------------------------------------------


@dataclass
class _Shown:
    """What the page displays: the client's session and window, and the
    active pane of that window with its size."""

    session: board_sweep.ClientSession
    pane: str
    cols: int
    rows: int


class Unsupported(Exception):
    """The tmux server is older than the bridge needs."""

    def __init__(self, version: str) -> None:
        super().__init__(version)
        self.version = version


class NativeBridge:
    """One page, one tmux control client."""

    def __init__(self, websocket: WebSocket, tmux: Sequence[str] = ("tmux",)) -> None:
        self.ws = websocket
        self.tmux = list(tmux)
        self.client = ControlClient(
            self._on_output, self._on_notification, decode_output=False
        )
        self.report = classic.SessionReportState()
        self.page_socket = _PageSocket(self)
        self.out = _Outbox()
        self.shown: _Shown | None = None
        self.windows_sent: list | None = None
        self.session_queued: board_sweep.ClientSession | None = None
        # The displayed pane and what the page has of it.
        self.pane = ""
        self.reset_due = True  # the page must start this pane afresh
        self.sent_next = 0  # the next history row the page lacks
        self.added_seen = 0  # history_added at the last update
        # Rows that scrolled in between two updates, last time: the margin a
        # capture aims with, since its range is relative to the screen top.
        self.scrolled = 0
        self.generation: int | None = None
        self.last_screen: tuple | None = None
        self.dirty = asyncio.Event()  # the pane wrote, or its view changed
        self.room = asyncio.Event()  # the page acknowledged enough
        self.room.set()
        self.state_dirty = asyncio.Event()  # session / windows may have changed
        self.prefixes: set[str] = set()
        self.closing = False
        self._tasks: list[asyncio.Task] = []
        self._side: set[asyncio.Task] = set()
        self._input_lock = asyncio.Lock()
        self._grid_lock = asyncio.Lock()

    # -- attach and teardown ----------------------------------------------------

    def attach_argv(self, sessions, preferred) -> list[str]:
        ladder = reconnect_argv(sessions, preferred)
        return [
            *server_spawn_prefix(bool(sessions)),
            *self.tmux,
            "-C",
            *tmux_conf_args(),
            *ladder[1:],
        ]

    async def start(self, argv: list[str], cwd: str) -> None:
        """Attach. Raises Unsupported when the server it reached is too old
        (the client is detached again), ControlError when it fails."""
        env = terminal_process_env(os.environ, term="xterm-256color")
        await self.client.start(argv, env, cwd=cwd)
        version, probe = await self.client.command(
            "display -p '#{version}'", f"display -p {quote(_PANE_FMT)}"
        )
        raw = version.text()[0] if version.ok and version.lines else ""
        modes = PaneModes.parse(probe.text()[0]) if probe.ok and probe.lines else None
        if (
            not supported(parse_tmux_version(raw))
            or modes is None
            or not modes.complete()
        ):
            await self.client.close()
            raise Unsupported(raw)
        prefixes = await self.client.command("show -gv prefix", "show -gv prefix2")
        self.prefixes = {
            line
            for reply in prefixes
            if reply.ok
            for line in reply.text()
            if line and line != "None"
        }
        # Window list changes and agent-state pills come as notifications.
        await self.client.command("refresh-client -B 'merlin-state:@*:#{@agent_state}'")

    async def run(self) -> None:
        self.report.agent = classic.short_agent(self.ws.headers.get("user-agent", ""))
        # broadcast_control only calls send_text and close on it.
        self.report.websocket = cast(WebSocket, self.page_socket)
        classic._client_views.add(self.report)
        self._tasks = [
            asyncio.create_task(self._sender()),
            asyncio.create_task(self._syncer()),
            asyncio.create_task(self._updater()),
            asyncio.create_task(self._receiver()),
            asyncio.create_task(self.client.exited.wait()),
        ]
        self.state_dirty.set()
        try:
            await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await self.close()

    async def close(self) -> None:
        classic._client_views.discard(self.report)
        self.report.outbox.clear()
        if self.report.sender is not None:
            self.report.sender.cancel()
        tasks = [*self._tasks, *self._side]
        current = asyncio.current_task()
        for task in tasks:
            if task is not current:
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(
                *(t for t in tasks if t is not current), return_exceptions=True
            )
        await self.client.close()

    async def close_socket(self, code: int, reason: str) -> None:
        """Close the page's socket (it reconnects and resynchronises); the
        receiver then ends and ``run`` tears the bridge down."""
        if self.closing:
            return
        self.closing = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.ws.close(code=code, reason=reason), 2.0)
        for task in self._tasks:
            task.cancel()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._side.add(task)
        task.add_done_callback(self._side.discard)

    # -- tmux to page -------------------------------------------------------------

    def _on_output(self, pane: str, _data: bytes) -> None:
        if pane == self.pane:
            self.dirty.set()

    def _on_notification(self, name: str, rest: bytes) -> None:
        if name == "exit":
            self._spawn(self.close_socket(1000, "tmux client exited"))
            return
        if name in (
            "session-changed",
            "session-window-changed",
            "window-pane-changed",
            "window-add",
            "window-close",
            "window-renamed",
            "unlinked-window-close",
            "layout-change",
            "subscription-changed",
            "session-renamed",
        ):
            self.state_dirty.set()
        elif name == "sessions-changed":
            self.push_control({"type": "sessions-changed"})
        elif name == "paste-buffer-changed" and (
            rest == CLIPBOARD_BUFFER.encode() or rest.startswith(b"buffer")
        ):
            # merlin-clip's buffer, or one tmux made from a program's OSC 52
            # (set-clipboard on) or a copy: the page's clipboard.
            self._spawn(self._send_clipboard(rest.decode("utf-8", "replace")))

    async def _send_clipboard(self, name: str) -> None:
        """Hand a paste buffer to the page, exactly (``save-buffer`` to a
        private file: a reply block cannot carry a final newline)."""
        fd, path = tempfile.mkstemp(prefix="merlin-clip-")
        os.close(fd)
        try:
            (saved,) = await self.client.command(
                f"save-buffer -b {quote(name)} {quote(path)}"
            )
            if not saved.ok:
                return
            with open(path, "rb") as f:
                data = f.read(CLIPBOARD_MAX + 1)
            if len(data) > CLIPBOARD_MAX:
                return
            self.push_control(
                {"type": "clipboard", "text": data.decode("utf-8", "replace")}
            )
        except ControlError:
            return
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    # -- admission ------------------------------------------------------------------

    def ack(self, total: int) -> None:
        """The page has handled the first ``total`` bytes of frames sent on
        this socket."""
        if total > self.out.acked:
            self.out.acked = min(total, self.out.sent)
        if self.out.unacked() <= LOW_WATER:
            self.room.set()

    def admit(self, size: int) -> bool:
        """Whether a frame of ``size`` bytes may join the window now: under
        HIGH_WATER, or alone when nothing is outstanding."""
        unacked = self.out.unacked()
        if unacked == 0 or unacked + size <= HIGH_WATER:
            return True
        self.room.clear()
        return False

    def push_control(
        self, payload: dict, on_sent: Callable[[], None] | None = None
    ) -> bool:
        return self.push_control_text("\x00" + json.dumps(payload), on_sent)

    def push_control_text(
        self, text: str, on_sent: Callable[[], None] | None = None
    ) -> bool:
        """Queue a control frame. A page so far behind that even control
        frames pile up is closed (it reconnects); False then."""
        return self._push(_Frame(text.encode("utf-8"), text=True, on_sent=on_sent))

    def _push(self, frame: _Frame) -> bool:
        if self.out.unacked() + len(frame.data) > HARD_LIMIT:
            self._spawn(self.close_socket(CLOSE_STALLED, "page stopped reading"))
            return False
        self.out.push(frame)
        return True

    async def _sender(self) -> None:
        while True:
            await self.out.wake.wait()
            self.out.wake.clear()
            while self.out.frames:
                frame = self.out.frames.popleft()
                size = len(frame.data)
                self.out.queued -= size
                self.out.sent += size
                if frame.text:
                    send = self.ws.send_text(frame.data.decode("utf-8"))
                else:
                    send = self.ws.send_bytes(frame.data)
                await asyncio.wait_for(send, SEND_TIMEOUT)
                if frame.on_sent is not None:
                    frame.on_sent()

    # -- the session and its windows -------------------------------------------------

    async def _syncer(self) -> None:
        """Bring the page in line with tmux whenever a notification says
        something may have changed: the session frame, the window strip, and
        the displayed pane."""
        while True:
            await self.state_dirty.wait()
            # Coalesce a burst of notifications into one read.
            await asyncio.sleep(0.02)
            self.state_dirty.clear()
            try:
                await self.sync()
            except ControlError:
                return

    async def sync(self) -> None:
        current, windows = await self.client.command(
            f"display -p {quote(_CLIENT_FMT)}", f"list-windows -F {quote(_WINDOW_FMT)}"
        )
        if not current.ok or not current.lines:
            return
        shown = _parse_shown(current.text()[0])
        if shown is None:
            return
        self._report_session(shown.session)
        if windows.ok:
            rows = [_parse_window(line) for line in windows.text()]
            listing = [w for w in rows if w is not None]
            if listing != self.windows_sent:
                self.windows_sent = listing
                self.push_control(
                    {
                        "type": "windows",
                        "session_id": shown.session.session_id,
                        "windows": listing,
                    }
                )
        previous = self.shown
        self.shown = shown
        if previous is None or previous.pane != shown.pane:
            self.show(shown.pane)
        elif (previous.cols, previous.rows) != (shown.cols, shown.rows):
            self.dirty.set()

    def show(self, pane: str) -> None:
        """Display ``pane``: the page starts it afresh at the next update."""
        self.pane = pane
        self.reset_due = True
        self.last_screen = None
        self.dirty.set()

    def _report_session(self, current: board_sweep.ClientSession) -> None:
        """The classic ``session`` control frame, through the outbox. What the
        page displays (``SessionReportState.last_reported``, read by
        ``looking_at``) changes when the frame has actually been sent."""
        if current == self.session_queued:
            return
        self.session_queued = current

        def sent() -> None:
            self.report.last_reported = current

        self.push_control(
            {
                "type": "session",
                "name": current.name,
                "id": current.session_id,
                "created": current.created,
                "window_id": current.window_id,
                "window_index": current.window_index,
                "window": current.window_name,
            },
            on_sent=sent,
        )

    # -- the grid ----------------------------------------------------------------------

    async def _updater(self) -> None:
        """Mirror the displayed pane: when it changed and the page has room,
        at most every UPDATE_INTERVAL."""
        last = 0.0
        while True:
            await self.dirty.wait()
            await self.room.wait()
            wait = last + UPDATE_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.dirty.clear()
            try:
                await self.update()
            except ControlError:
                return
            last = time.monotonic()

    async def update(self) -> None:
        """One update of the displayed pane: its state, the history rows the
        page lacks (up to a batch), and its screen, read in one command line."""
        pane = self.pane
        if not pane:
            return
        async with self._grid_lock:
            reset = self.reset_due
            commands = [f"display -p -t {pane} {quote(_PANE_FMT)}"]
            if reset:
                # The last INITIAL_HISTORY rows (tmux clamps to what exists).
                commands.append(capture(pane, -INITIAL_HISTORY, -1))
            else:
                # Aim from where the pane is now: a capture streams its rows
                # with attributes carried from row to row, so every row from
                # its first one is sent (the page decodes them all and skips
                # the ones it has), and a tight aim keeps those few.
                (now,) = await self.client.command(
                    f"display -p -t {pane} '#{{history_added}}'"
                )
                if now.ok and now.lines and now.text()[0].isdigit():
                    self.added_seen = int(now.text()[0])
                if self.added_seen > self.sent_next:
                    # From the overlap row (the one before the first row the
                    # page lacks), with a margin for the rows that may scroll
                    # in before the capture runs.
                    margin = 2 * self.scrolled + 2
                    start = -(self.added_seen - self.sent_next + 1) - margin
                    end = min(-1, start + ROWS_PER_CAPTURE + margin)
                    commands.append(capture(pane, start, end))
            commands.append(capture(pane, -1, "-"))
            aimed = self.added_seen
            replies = await self.client.command(*commands)
            if pane != self.pane:
                return  # the page moved on meanwhile
            if not all(r.ok for r in replies) or not replies[0].lines:
                return
            modes = PaneModes.parse(replies[0].text()[0])
            if modes is None:
                return
            added = modes.added
            collected = modes.num("history_collected")
            generation = modes.num("history_generation")
            if not reset and generation != self.generation:
                # Cleared or reflowed (a resize): what the page has is stale.
                self.reset_due = True
                self.dirty.set()
                return
            # Rows that scrolled in between the aim and the capture.
            self.scrolled = max(0, added - aimed) if not reset else 0
            captured = parse_rows(replies[1].lines, added) if len(replies) == 3 else []
            screen_all = parse_rows(replies[-1].lines, added)
            overlap = next((r for r in screen_all if r.number == added - 1), None)
            screen_end = next(
                (
                    i + 1
                    for i, r in enumerate(screen_all)
                    if r.number == added + modes.rows - 1
                ),
                len(screen_all),
            )
            screen_stream = screen_all[:screen_end]
            screen_skip = sum(1 for r in screen_stream if r.number < added)
            screen = screen_stream[screen_skip:]
            # Nothing about what the page has changes until the frame is in
            # the outbox: a frame that is not admitted leaves it all due.
            gap = 0
            cont = False
            next_row = self.sent_next
            if reset:
                first = 0
                kept = [r for r in captured if r.number < added]
                start_row = kept[0].number if kept else added
            else:
                if next_row < collected:
                    # tmux dropped rows the page never got (its history
                    # limit): say so, and carry on from what is left.
                    gap = collected - next_row
                    next_row = collected
                first = next(
                    (i for i, r in enumerate(captured) if r.number == next_row), None
                )
                if first is None:
                    kept = []
                    if next_row < added:
                        # More rows scrolled in than aimed for: none sent
                        # now, retried at once aimed afresh.
                        self.dirty.set()
                else:
                    kept = [r for r in captured[first:] if r.number < added]
                before = captured[first - 1] if first else None
                cont = bool(
                    before and before.number == next_row - 1 and before.wrapped and kept
                )
                start_row = next_row
            # A batch at most HISTORY_BYTES_MAX: the stream is cut at its end
            # only (each row's attributes may come from the rows before it);
            # the rest goes in the next update.
            total = sum(len(r.data) + 1 for r in captured[: first or 0])
            for i, r in enumerate(kept):
                total += len(r.data) + 1
                if total > HISTORY_BYTES_MAX and i > 0:
                    kept = kept[:i]
                    self.dirty.set()
                    break
            hist_stream = captured[: (first or 0) + len(kept)] if kept else []
            hist_skip = (first or 0) if kept else 0
            state = modes.state()
            screen_key = (
                tuple(r.data for r in screen_stream),
                _flags(screen),
                json.dumps(state, sort_keys=True),
            )
            if not reset and not kept and not gap and screen_key == self.last_screen:
                return
            header = {
                **state,
                "reset": reset,
                "hist": {
                    "start": start_row,
                    "count": len(kept),
                    "skip": hist_skip,
                    "wrapped": _flags(kept),
                    "cont": cont,
                    "gap": gap,
                },
                "screen": {
                    "skip": screen_skip,
                    "wrapped": _flags(screen),
                    "cont": bool(overlap and overlap.wrapped),
                },
            }
            frame = _frame(
                FRAME_UPDATE, header, [r.data for r in hist_stream + screen_stream]
            )
            if not self.admit(len(frame)):
                # No room: nothing sent, everything still in tmux; the next
                # update runs once the page acknowledged enough.
                self.dirty.set()
                return
            if not self._push(_Frame(frame)):
                return
            self.reset_due = False
            self.generation = generation
            if kept:
                self.sent_next = kept[-1].number + 1
            elif reset:
                self.sent_next = added
            else:
                self.sent_next = next_row
            self.added_seen = max(added, self.sent_next)
            self.last_screen = screen_key
            if self.sent_next < added:
                # More history is due than one capture holds.
                self.dirty.set()

    async def history(self, pane: str, end: int, count: int) -> None:
        """Rows ``[end - count, end)`` of ``pane``'s history by absolute
        number, at most HISTORY_BYTES_MAX (fewer rows asked for until they
        fit). The capture is sent from its first row (attributes carry from row
        to row; ``skip`` says how many the page decodes without keeping). A
        page that is behind gets ``busy`` and asks again later."""
        if not _PANE_ID.fullmatch(pane) or count <= 0:
            return
        count = min(count, HISTORY_PAGE_MAX)
        if not self.admit(4096):
            self._send_history(pane, end, end, [], 0, None, busy=True)
            return
        margin = 2
        for _ in range(8):
            (now,) = await self.client.command(
                f"display -p -t {pane} '#{{history_added}}'"
            )
            if not now.ok or not now.lines or not now.text()[0].isdigit():
                return
            estimate = int(now.text()[0])
            start = max(0, end - count)
            first_rel = -(estimate - start) - margin
            last_rel = min(-1, -(estimate - end) - 1)
            info, reply = await self.client.command(
                f"display -p -t {pane} "
                "'#{history_added}\t#{history_collected}\t#{history_generation}'",
                capture(pane, first_rel, last_rel),
            )
            if not info.ok or not reply.ok or not info.lines:
                return
            added, collected, generation = (int(x) for x in info.text()[0].split("\t"))
            start = max(start, collected)
            if end <= start:
                self._send_history(pane, start, start, [], 0, generation)
                return
            rows = [r for r in parse_rows(reply.lines, added) if r.number < end]
            first = next((i for i, r in enumerate(rows) if r.number == start), None)
            if first is None or rows[-1].number != end - 1:
                margin = 2 * max(0, added - estimate) + 2
                continue  # the pane scrolled meanwhile: aim again
            if sum(len(r.data) + 1 for r in rows) > HISTORY_BYTES_MAX and count > 1:
                count //= 2
                continue
            self._send_history(pane, start, end, rows, first, generation)
            return

    def _send_history(
        self,
        pane: str,
        start: int,
        end: int,
        rows: list[Row],
        skip: int,
        generation: int | None,
        *,
        busy: bool = False,
    ) -> None:
        """``rows`` is the capture from its first row: the first ``skip``
        are decoded by the page and not kept."""
        header: dict = {
            "pane": pane,
            "start": start,
            "end": end,
            "skip": skip,
            "generation": generation,
            "wrapped": _flags(rows[skip:]),
        }
        if busy:
            header["busy"] = True
        self._push(_Frame(_frame(FRAME_HISTORY, header, [r.data for r in rows])))

    # -- page to tmux -------------------------------------------------------------

    async def _receiver(self) -> None:
        while True:
            message = await self.ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                await self.handle(parsed)

    async def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        try:
            if kind == "input":
                await self.send_input(
                    str(msg.get("pane", "")), str(msg.get("data", ""))
                )
            elif kind == "paste":
                await self.paste(str(msg.get("pane", "")), str(msg.get("data", "")))
            elif kind == "key":
                await self.send_key(str(msg.get("pane", "")), str(msg.get("key", "")))
            elif kind == "resize":
                cols = min(max(int(msg.get("cols", 120)), 1), 500)
                rows = min(max(int(msg.get("rows", 40)), 1), 500)
                await self.client.command(f"refresh-client -C {cols}x{rows}")
            elif kind == "ack":
                self.ack(int(msg.get("bytes", 0)))
            elif kind == "switch":
                await self.switch(str(msg.get("target", "")))
            elif kind == "history":
                await self.history(
                    str(msg.get("pane", "")),
                    int(msg.get("end", 0)),
                    int(msg.get("count", 0)),
                )
            elif kind == "resync":
                # The page lost its view: start the pane afresh.
                if self.pane:
                    self.show(self.pane)
            elif kind == "visibility":
                self.report.visible = bool(msg.get("visible", True))
                self.report.focused = bool(msg.get("focused", True))
            elif kind == "ping":
                self.push_control({"type": "pong"})
            elif kind == "clipboard_sync":
                classic._sync_clipboard(str(msg.get("text", "")))
        except (ValueError, TypeError):
            return
        except ControlError as e:
            logger.info("Native terminal command failed: %s", e)

    async def send_input(self, pane: str, data: str) -> None:
        if not _PANE_ID.fullmatch(pane) or not data:
            return
        raw = data.encode("utf-8")
        commands = [
            f"send-keys -t {pane} -H {hex_keys(raw[i : i + INPUT_CHUNK])}"
            for i in range(0, len(raw), INPUT_CHUNK)
        ]
        async with self._input_lock:
            for i in range(0, len(commands), INPUT_COMMANDS_PER_LINE):
                await self.client.command(*commands[i : i + INPUT_COMMANDS_PER_LINE])

    async def paste(self, pane: str, data: str) -> None:
        if not _PANE_ID.fullmatch(pane) or not data:
            return
        raw = data.encode("utf-8")
        if len(raw) > PASTE_MAX:
            self.push_control(
                {"type": "paste_refused", "reason": "Paste too large (1 MB max)"}
            )
            return
        name = f"merlin-paste-{secrets.token_hex(6)}"
        fd, path = tempfile.mkstemp(prefix="merlin-paste-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            async with self._input_lock:
                await self.client.command(
                    f"load-buffer -b {name} {quote(path)}",
                    f"paste-buffer -d -p -r -b {name} -t {pane}",
                )
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    async def send_key(self, pane: str, key: str) -> None:
        if not _PANE_ID.fullmatch(pane) or not _KEY_NAME.fullmatch(key):
            return
        name = quote(key)
        async with self._input_lock:
            table, bound = await self.client.command(
                "display -p '#{client_key_table}'", f"list-keys -T root {name}"
            )
            in_table = table.ok and table.text() not in ([], ["root"])
            if in_table or bound.ok or key in self.prefixes:
                # As if typed: the binding (or the prefix) runs, on this
                # client and its current pane.
                await self.client.command(f"send-keys -K {name}")
            else:
                await self.client.command(f"send-keys -t {pane} {name}")

    async def switch(self, target: str) -> None:
        if not target or "\n" in target:
            return
        (switched,) = await self.client.command(f"switch-client -t {quote(target)}")
        if switched.ok and ":" in target:
            # As for the classic terminal: select-window fires the window
            # hooks even when the window is already current.
            await self.client.command(f"select-window -t {quote(target)}")
        await self.sync()


def _parse_shown(line: str) -> _Shown | None:
    parts = line.split("\t")
    if len(parts) != 9:
        return None
    (
        name,
        session_id,
        created,
        window_id,
        window_index,
        window_name,
        pane,
        cols,
        rows,
    ) = parts
    if not (
        created.isdigit()
        and window_index.isdigit()
        and cols.isdigit()
        and rows.isdigit()
    ):
        return None
    if not _PANE_ID.fullmatch(pane):
        return None
    session = board_sweep.ClientSession(
        name, session_id, int(created), window_id, int(window_index), window_name
    )
    return _Shown(session, pane, int(cols), int(rows))


def _parse_window(line: str) -> dict | None:
    parts = line.split("\t")
    if len(parts) != 5 or not parts[1].isdigit():
        return None
    window_id, index, name, active, state = parts
    return {
        "id": window_id,
        "index": int(index),
        "name": name,
        "active": active == "1",
        "state": state,
    }


# -- the endpoint -----------------------------------------------------------------


async def _refuse(websocket: WebSocket, version: str) -> None:
    with contextlib.suppress(Exception):
        await websocket.send_text(
            "\x00"
            + json.dumps(
                {
                    "type": "unsupported",
                    "version": version,
                    "minimum": ".".join(map(str, MIN_TMUX)),
                }
            )
        )
        await websocket.close(code=CLOSE_UNSUPPORTED, reason="tmux too old")


async def native_ws(websocket: WebSocket) -> None:
    if not verify_ws_cookie(websocket):
        await websocket.close(code=4401, reason="Unauthorized")
        return
    await websocket.accept()
    preferred = parse_session_identity(
        websocket.query_params.get("session_id"),
        websocket.query_params.get("session_created"),
    )
    loop = asyncio.get_running_loop()
    try:
        sessions = await asyncio.wait_for(
            loop.run_in_executor(None, board_sweep.run_session_sweep_checked),
            PROBE_TIMEOUT * 2,
        )
    except asyncio.TimeoutError:
        sessions = None
    if sessions is None:
        await websocket.close(code=1013, reason="tmux temporarily unavailable")
        return
    if not sessions:
        # No server: the installed binary will create it.
        raw = await binary_version()
        if not supported(parse_tmux_version(raw)):
            await _refuse(websocket, raw)
            return
    bridge = NativeBridge(websocket)
    try:
        await bridge.start(
            bridge.attach_argv(sessions, preferred),
            classic._cwd or str(classic.PROJECT_ROOT),
        )
    except Unsupported as e:
        await _refuse(websocket, e.version)
        return
    except ControlError:
        logger.exception("Native terminal attach failed")
        await bridge.client.close()
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason="tmux attach failed")
        return
    logger.info("Native terminal connected: pid=%s", bridge.client.pid)
    try:
        await bridge.run()
    except WebSocketDisconnect:
        pass
    logger.info("Native terminal disconnected")
