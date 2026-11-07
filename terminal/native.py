"""Native terminal: the ``/ws/terminal2`` WebSocket, a tmux control-mode bridge.

The classic terminal (``/ws/terminal``, ``terminal/routes.py``) runs a tmux
client on a PTY and streams the screen tmux draws. This bridge attaches a tmux
client in control mode instead (``terminal/control.py``): tmux draws nothing
and reports each pane's raw output, and the page parses it and keeps the
history itself, so it can scroll and select natively. Both terminals attach to
the same tmux server and can be open at once.

What the bridge guarantees (pinned by ``tests/unit/test_terminal_native.py``):

- **One pane on the wire.** The page shows the active pane of its client's
  current window. Only that pane's output is forwarded; every other pane's
  output is dropped here (after updating its ``StreamState``). Nothing is ever
  paused in tmux: a pane paused or turned off by its only client stops being
  read, which would freeze the program writing to it (Phase 1 decision record).
- **Snapshots are exact.** Showing a pane starts with a snapshot: its cells,
  modes, recent history, pending-wrap state and the state tmux does not report
  (``terminal/stream_state.py``: rendition, charsets, an unfinished UTF-8
  character), captured in one command line, then the live stream. Output
  numbered up to the snapshot reply's ``seq`` is in the capture, output after
  it is not (``ControlClient`` orders both on one stream).
- **Bounded memory per page.** Every frame the page gets (output, snapshot,
  history, control) counts against one window of unacknowledged bytes; the
  page acknowledges what it has handled. Pane output past ``HIGH_WATER`` is
  dropped and replaced by a fresh snapshot once the page is back under
  ``LOW_WATER``; a snapshot or a history range waits for room, and is capped in
  size; output held during a capture is capped too. A page that stops reading
  entirely is disconnected (``HARD_LIMIT``), so it reconnects and
  resynchronises. One sender writes every frame, in order, each within
  ``SEND_TIMEOUT``.
- **Input exactly once.** Text goes as ``send-keys -H`` in 1 KiB commands, a
  paste through a private paste buffer with ``paste-buffer -p`` (tmux adds the
  bracketed-paste markers when the program asked for them), a named key as
  ``send-keys``: with ``-K`` when tmux binds it (the prefix, a root binding such
  as ``Escape`` or ``F2``, any key while a key table other than root is active),
  so the binding runs exactly as for a typed key; plainly otherwise, so tmux
  encodes it for the pane's own modes.
- **The running server is checked.** The control connection asks the server it
  attached to for its version; below 3.8 the page gets an ``unsupported``
  notice and falls back to the classic terminal. The installed binary is only
  checked when no server runs (it would create one).
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
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import cast

from fastapi import WebSocket, WebSocketDisconnect

from auth import verify_ws_cookie
from board import sweep as board_sweep
from terminal import routes as classic
from terminal.control import ControlClient, ControlError, Reply, hex_keys, quote
from terminal.stream_state import StreamState
from terminal.tmux import (
    parse_session_identity,
    reconnect_argv,
    server_spawn_prefix,
    terminal_process_env,
    tmux_conf_args,
)

logger = logging.getLogger("merlin.terminal")

MIN_TMUX = (3, 8)

# Binary frames to the page: one kind byte, then the payload.
FRAME_OUTPUT = 0x01  # + pane id + NUL + raw pane bytes
FRAME_SNAPSHOT = 0x02  # + JSON header + NUL + bytes to replay into a reset parser
FRAME_HISTORY = 0x03  # + JSON header + NUL + captured history lines

# The flow-control window, in bytes the page has not acknowledged yet.
HIGH_WATER = 1024 * 1024
LOW_WATER = 256 * 1024
# Past this the page is not reading at all: close it, it reconnects.
HARD_LIMIT = 4 * HIGH_WATER
FRAME_MAX = 256 * 1024
HELD_MAX = HIGH_WATER
SNAPSHOT_MAX = 512 * 1024
HISTORY_BYTES_MAX = 256 * 1024
CLIPBOARD_MAX = 1024 * 1024
SEND_TIMEOUT = 10.0
PROBE_TIMEOUT = 5.0
INPUT_CHUNK = 1024
INPUT_COMMANDS_PER_LINE = 8
PASTE_MAX = 1024 * 1024
INITIAL_HISTORY = 1000
HISTORY_PAGE_MAX = 2000
CLOSE_UNSUPPORTED = 4426
CLOSE_STALLED = 4408
CLIPBOARD_BUFFER = "merlin-clip"

_PANE_ID = re.compile(r"%\d+")
_KEY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}")
_VERSION = re.compile(r"(\d+)\.(\d+)")

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
    "alternate_saved_x",
    "alternate_saved_y",
    "keypad_cursor_flag",
    "keypad_flag",
    "insert_flag",
    "wrap_flag",
    "origin_flag",
    "scroll_region_upper",
    "scroll_region_lower",
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


# -- snapshot ---------------------------------------------------------------------


@dataclass(frozen=True)
class PaneModes:
    """A pane's state as tmux reports it, enough to put a fresh parser in the
    same modes."""

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
    def pending_wrap(self) -> bool:
        """The cursor sits past the last column: the next character wraps.
        tmux reports that as ``cursor_x == pane_width``."""
        return self.flag("wrap_flag") and self.num("cursor_x") >= self.cols


_CURSOR_STYLES = {"block": 1, "underline": 3, "bar": 5}


def _cup(row: int, col: int) -> bytes:
    return b"\x1b[%d;%dH" % (row + 1, col + 1)


def mode_sequences(modes: PaneModes, cursor_row: bytes | None = None) -> bytes:
    """Escape sequences that put a reset parser in the pane's modes and place
    the cursor. The scroll region and origin mode come first (both move the
    cursor home), the cursor next, insert mode only after the cursor row may
    have been rewritten. With a pending wrap, ``cursor_row`` (that row exactly,
    ``capture-pane -e -N``) is written again from column 0, which leaves the
    parser in the same pending-wrap state."""
    out = bytearray()
    rows = modes.rows
    upper = modes.num("scroll_region_upper", 0)
    lower = modes.num("scroll_region_lower", rows - 1)
    if (upper, lower) != (0, rows - 1) and 0 <= upper < lower < rows:
        out += b"\x1b[%d;%dr" % (upper + 1, lower + 1)
    if modes.flag("origin_flag"):
        out += b"\x1b[?6h"
    if not modes.flag("wrap_flag"):
        out += b"\x1b[?7l"
    row = modes.num("cursor_y")
    if modes.flag("origin_flag"):
        row -= upper
    if modes.pending_wrap and cursor_row is not None:
        out += _cup(row, 0) + cursor_row
    else:
        out += _cup(row, min(modes.num("cursor_x"), modes.cols - 1))
    if modes.flag("insert_flag"):
        out += b"\x1b[4h"
    if modes.flag("keypad_cursor_flag"):
        out += b"\x1b[?1h"
    if modes.flag("keypad_flag"):
        out += b"\x1b="
    for name, mode in (
        ("mouse_standard_flag", 1000),
        ("mouse_button_flag", 1002),
        ("mouse_all_flag", 1003),
        ("mouse_utf8_flag", 1005),
        ("mouse_sgr_flag", 1006),
    ):
        if modes.flag(name):
            out += b"\x1b[?%dh" % mode
    if modes.flag("bracket_paste_flag"):
        out += b"\x1b[?2004h"
    style = _CURSOR_STYLES.get(modes.values.get("cursor_shape", ""))
    if style is not None:
        out += b"\x1b[%d q" % (style if modes.flag("cursor_blinking") else style + 1)
    if not modes.flag("cursor_flag"):
        out += b"\x1b[?25l"
    return bytes(out)


def build_replay(
    modes: PaneModes,
    lines: Sequence[bytes],
    alt_screen: Sequence[bytes] | None = None,
    *,
    cursor_row: bytes | None = None,
) -> bytes:
    """The bytes that rebuild a pane in a reset parser of its size.

    ``lines`` are the main screen's history and its visible rows, captured
    with ``-J`` (soft wraps joined, the parser wraps them again at the same
    width): written one per line, so the visible rows land on the screen and
    the rest scrolls into the parser's history. Each row is written whole: the
    cursor may sit at column 0 of a row already written (after a carriage
    return), so the cursor is placed afterwards, never by truncating.

    With the alternate screen on, ``alt_screen`` holds its rows (row-exact):
    the main screen is written first with the saved cursor, the alternate
    screen entered, its rows drawn in place. Then the modes and the cursor
    (``mode_sequences``). The bridge then appends the state tmux does not
    report (``StreamState.restore``: rendition, charsets, an unfinished UTF-8
    character) and the escape sequence cut in the middle (``capture-pane
    -P``), in that order, so the live bytes complete it.
    """
    # capture-pane -e writes line-drawing cells between SO and SI: G1 is the
    # DEC graphics set while the captured rows are written (``state`` puts
    # the program's own charsets back afterwards).
    out = bytearray(b"\x1b)0")
    out += b"\r\n".join(line + b"\x1b[0m" for line in lines)
    if alt_screen is not None:
        out += _cup(modes.num("alternate_saved_y"), modes.num("alternate_saved_x"))
        out += b"\x1b[?1049h\x1b[H\x1b[2J"
        for row, line in enumerate(alt_screen[: modes.rows]):
            out += _cup(row, 0) + line + b"\x1b[0m"
    out += mode_sequences(modes, cursor_row)
    return bytes(out)


def _frame(kind: int, header: dict, body: bytes) -> bytes:
    return (
        bytes([kind])
        + json.dumps(header, separators=(",", ":")).encode()
        + b"\x00"
        + body
    )


# -- the outbox -----------------------------------------------------------------------


@dataclass
class _Frame:
    data: bytes | bytearray
    text: bool = False
    on_sent: Callable[[], None] | None = None

    @property
    def is_output(self) -> bool:
        return not self.text and self.data[:1] == b"\x01"


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

    def drop_output(self) -> None:
        kept = deque(f for f in self.frames if not f.is_output)
        self.frames = kept
        self.queued = sum(len(f.data) for f in kept)


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
        self.client = ControlClient(self._on_output, self._on_notification)
        self.report = classic.SessionReportState()
        self.page_socket = _PageSocket(self)
        self.out = _Outbox()
        self.shown: _Shown | None = None
        self.pane = ""
        self.windows_sent: list | None = None
        self.session_queued: board_sweep.ClientSession | None = None
        self.states: dict[str, StreamState] = {}
        self.session_id = ""
        self.overflow = False
        self.resync_wanted = False
        self.snapshotting = False
        self.held: list[tuple[int, bytes]] = []
        self.held_bytes = 0
        self.spoiled = False
        self.dirty = asyncio.Event()
        self.prefixes: set[str] = set()
        self.closing = False
        self._tasks: list[asyncio.Task] = []
        self._snapshot_lock = asyncio.Lock()
        self._input_lock = asyncio.Lock()
        self._resync: asyncio.Task | None = None
        self._side: set[asyncio.Task] = set()

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
        await self._register_panes()

    async def _register_panes(self) -> None:
        """Panes that existed before this client saw their output: their
        rendition is not known until their next reset."""
        (panes,) = await self.client.command("list-panes -s -F '#{pane_id}'")
        if panes.ok:
            for pane in panes.text():
                self.states.setdefault(pane, StreamState(known=False))

    async def run(self) -> None:
        self.report.agent = classic.short_agent(self.ws.headers.get("user-agent", ""))
        # broadcast_control only calls send_text and close on it.
        self.report.websocket = cast(WebSocket, self.page_socket)
        classic._client_views.add(self.report)
        self._tasks = [
            asyncio.create_task(self._sender()),
            asyncio.create_task(self._syncer()),
            asyncio.create_task(self._receiver()),
            asyncio.create_task(self.client.exited.wait()),
        ]
        self.dirty.set()
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
        if self._resync is not None:
            tasks.append(self._resync)
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

    def _state(self, pane: str) -> StreamState:
        state = self.states.get(pane)
        if state is None:
            # First output of a pane created while attached: known from birth.
            state = self.states[pane] = StreamState(known=True)
        return state

    def _on_output(self, pane: str, data: bytes) -> None:
        if pane != self.pane:
            self._state(pane).feed(data)
            return
        if self.snapshotting:
            if self.spoiled or self.held_bytes + len(data) > HELD_MAX:
                # Too much arrived during the capture: this snapshot will be
                # discarded and retried, so keep the stream state exact now.
                if not self.spoiled:
                    self.spoiled = True
                    for _, chunk in self.held:
                        self._state(pane).feed(chunk)
                    self.held, self.held_bytes = [], 0
                self._state(pane).feed(data)
                return
            self.held.append((self.client.seq, data))
            self.held_bytes += len(data)
            return
        self._state(pane).feed(data)
        if self.overflow:
            return
        self._forward(data)

    def _forward(self, data: bytes) -> None:
        if self.out.unacked() + len(data) > HIGH_WATER:
            # The page is behind: stop forwarding, drop until it catches up,
            # then resynchronise with a snapshot.
            self.overflow = True
            self.out.drop_output()
            self._maybe_recover()
            return
        prefix = bytes([FRAME_OUTPUT]) + self.pane.encode() + b"\x00"
        frames = self.out.frames
        last = frames[-1] if frames else None
        if (
            last is not None
            and isinstance(last.data, bytearray)
            and last.data.startswith(prefix)
            and len(last.data) + len(data) <= FRAME_MAX
        ):
            last.data += data
            self.out.queued += len(data)
            self.out.wake.set()
        else:
            self.out.push(_Frame(bytearray(prefix + data)))

    def _maybe_recover(self) -> None:
        """Start a fresh snapshot when the page needs one (after an overflow,
        or a snapshot that had to wait or was spoiled) and there is room."""
        if not (self.overflow or self.resync_wanted) or not self.pane:
            return
        if self.snapshotting or (self._resync is not None and not self._resync.done()):
            return
        if self.out.unacked() > LOW_WATER:
            return
        self._resync = asyncio.create_task(self._resnapshot())

    async def _resnapshot(self) -> None:
        """Snapshot until the page has a current view, while there is room.
        A capture spoiled by a flood is retried after a short pause."""
        while (
            (self.overflow or self.resync_wanted)
            and self.pane
            and self.out.unacked() <= LOW_WATER
        ):
            try:
                await self.snapshot(self.pane)
            except ControlError:
                return
            if self.overflow or self.resync_wanted:
                await asyncio.sleep(0.05)

    def push_control(
        self, payload: dict, on_sent: Callable[[], None] | None = None
    ) -> bool:
        return self.push_control_text("\x00" + json.dumps(payload), on_sent)

    def push_control_text(
        self, text: str, on_sent: Callable[[], None] | None = None
    ) -> bool:
        """Queue a control frame. A page so far behind that even control
        frames pile up is closed (it reconnects); False then."""
        data = text.encode("utf-8")
        if self.out.unacked() + len(data) > HARD_LIMIT:
            self._spawn(self.close_socket(CLOSE_STALLED, "page stopped reading"))
            return False
        self.out.push(_Frame(data, text=True, on_sent=on_sent))
        return True

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
            self.dirty.set()
        elif name == "sessions-changed":
            self.push_control({"type": "sessions-changed"})
        elif name == "paste-buffer-changed" and rest == CLIPBOARD_BUFFER.encode():
            self._spawn(self._send_clipboard())

    async def _send_clipboard(self) -> None:
        """``merlin-clip copy`` stores its text in a named paste buffer (see
        ``bin/merlin-clip``): hand it to the page, exactly (``save-buffer``
        to a private file, since a reply block cannot carry a final
        newline)."""
        fd, path = tempfile.mkstemp(prefix="merlin-clip-")
        os.close(fd)
        try:
            (saved,) = await self.client.command(
                f"save-buffer -b {CLIPBOARD_BUFFER} {quote(path)}"
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
                    send = self.ws.send_text(bytes(frame.data).decode("utf-8"))
                else:
                    send = self.ws.send_bytes(bytes(frame.data))
                await asyncio.wait_for(send, SEND_TIMEOUT)
                if frame.on_sent is not None:
                    frame.on_sent()

    # -- state ---------------------------------------------------------------------

    async def _syncer(self) -> None:
        """Bring the page in line with tmux whenever a notification says
        something may have changed: the session frame, the window strip, and a
        snapshot when the displayed pane or its size changed."""
        while True:
            await self.dirty.wait()
            # Coalesce a burst of notifications into one read.
            await asyncio.sleep(0.02)
            self.dirty.clear()
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
        if shown.session.session_id != self.session_id:
            self.session_id = shown.session.session_id
            await self._register_panes()
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
        if (
            previous is None
            or previous.pane != shown.pane
            or (previous.cols, previous.rows) != (shown.cols, shown.rows)
        ):
            await self.snapshot(shown.pane)

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

    async def snapshot(self, pane: str) -> None:
        """Send ``pane``'s state, then its live stream from where the capture
        left off. Waits for room in the window: a page that is behind gets the
        snapshot once it caught up (``_maybe_recover``)."""
        async with self._snapshot_lock:
            if self.pane != pane:
                # Output of the old pane is no longer wanted; the new pane's
                # output is dropped until its snapshot is sent.
                self.out.drop_output()
                self.pane = pane
                self.overflow = True
            if self.out.unacked() > LOW_WATER:
                self.resync_wanted = True
                return
            self.snapshotting = True
            self.spoiled = False
            self.held, self.held_bytes = [], 0
            try:
                result = await self._capture(pane)
            except ControlError:
                self._end_snapshot(pane, None)
                raise
            self._end_snapshot(pane, result)

    def _end_snapshot(
        self, pane: str, result: tuple[bytes, bytes, dict, int] | None
    ) -> None:
        held, self.held, self.held_bytes = self.held, [], 0
        self.snapshotting = False
        state = self._state(pane)
        if result is None or self.spoiled or self.pane != pane:
            for _, chunk in held:
                state.feed(chunk)
            self.spoiled = False
            if self.pane == pane:
                # The page's view is stale until a snapshot gets through.
                self.overflow = True
                self.resync_wanted = True
            self._maybe_recover()
            return
        replay, pending, header, seq = result
        before = [chunk for number, chunk in held if number <= seq]
        after = [chunk for number, chunk in held if number > seq]
        for chunk in before:
            state.feed(chunk)
        # The unreported state, then the escape sequence cut in the middle,
        # which the live bytes complete.
        replay += state.restore() + pending
        # Everything queued for this pane is older than the capture.
        self.out.drop_output()
        self.out.push(_Frame(_frame(FRAME_SNAPSHOT, header, replay)))
        self.overflow = False
        self.resync_wanted = False
        for chunk in after:
            state.feed(chunk)
            if not self.overflow:
                self._forward(chunk)

    async def _capture(self, pane: str) -> tuple[bytes, bytes, dict, int] | None:
        """The snapshot of ``pane`` (replay bytes without the stream state,
        header, reply seq), at most SNAPSHOT_MAX: history rows are halved
        until it fits."""
        history = INITIAL_HISTORY
        while True:
            result = await self._capture_once(pane, history)
            if result is None or len(result[0]) <= SNAPSHOT_MAX or history == 0:
                return result
            # Too big: the output since the first try is held, so the next
            # capture's seq still splits it exactly.
            history //= 4

    async def _capture_once(
        self, pane: str, history: int
    ) -> tuple[bytes, bytes, dict, int] | None:
        t = f"-t {pane}"
        start = f"-S -{history}" if history else "-S 0"
        main = await self.client.command(
            f"display -p {t} {quote(_PANE_FMT)}",
            f"capture-pane -p -e -J {start} -E - {t}",
            f"capture-pane -p -e -N {t}",
            f"capture-pane -p -P {t}",
        )
        modes = (
            PaneModes.parse(main[0].text()[0]) if main[0].ok and main[0].lines else None
        )
        if modes is None or not all(r.ok for r in main):
            return None
        if modes.flag("alternate_on"):
            # The main screen is saved aside (-a); the visible rows are the
            # alternate screen's. The history range is asked for only when
            # there is one: an empty range answers the first visible row.
            rows = min(history, modes.num("history_size"))
            hist = (
                f"capture-pane -p -e -J -S -{rows} -E -1 {t}"
                if rows
                else "display -p ''"
            )
            alt = await self.client.command(
                f"display -p {t} {quote(_PANE_FMT)}",
                hist,
                f"capture-pane -p -e -J -a {t}",
                f"capture-pane -p -e -N {t}",
                f"capture-pane -p -P {t}",
            )
            alt_modes = (
                PaneModes.parse(alt[0].text()[0])
                if alt[0].ok and alt[0].lines
                else None
            )
            if alt_modes is None:
                return None
            if not all(r.ok for r in alt):
                # The program left the alternate screen in between.
                return await self._capture_once(pane, history)
            modes, replies = alt_modes, alt
            lines = (alt[1].lines if rows else []) + alt[2].lines
            screen = alt[3].lines
            pending = _pending(alt[4])
            replay = build_replay(
                modes, lines, screen, cursor_row=_row(screen, modes.num("cursor_y"))
            )
        else:
            replies = main
            pending = _pending(main[3])
            replay = build_replay(
                modes,
                main[1].lines,
                None,
                cursor_row=_row(main[2].lines, modes.num("cursor_y")),
            )
        header = {
            "pane": pane,
            "cols": modes.cols,
            "rows": modes.rows,
            "alternate": modes.flag("alternate_on"),
            "history": {
                "added": modes.num("history_added"),
                "collected": modes.num("history_collected"),
                "generation": modes.num("history_generation"),
                "size": modes.num("history_size"),
            },
        }
        return replay, pending, header, replies[0].seq

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

    def ack(self, total: int) -> None:
        """The page has handled the first ``total`` bytes of frames sent on
        this socket."""
        if total > self.out.acked:
            self.out.acked = min(total, self.out.sent)
        self._maybe_recover()

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
        async with self._input_lock:
            table, bound = await self.client.command(
                "display -p '#{client_key_table}'", f"list-keys -T root {key}"
            )
            in_table = table.ok and table.text() not in ([], ["root"])
            if in_table or bound.ok or key in self.prefixes:
                # As if typed: the binding (or the prefix) runs, on this
                # client and its current pane.
                await self.client.command(f"send-keys -K {key}")
            else:
                await self.client.command(f"send-keys -t {pane} {key}")

    async def switch(self, target: str) -> None:
        if not target or "\n" in target:
            return
        (switched,) = await self.client.command(f"switch-client -t {quote(target)}")
        if switched.ok and ":" in target:
            # As for the classic terminal: select-window fires the window
            # hooks even when the window is already current.
            await self.client.command(f"select-window -t {quote(target)}")
        await self.sync()

    async def history(self, pane: str, end: int, count: int) -> None:
        """Rows ``[end - count, end)`` of ``pane``'s history, numbered from
        the pane's first line ever (``history_added`` counts them), at most
        HISTORY_BYTES_MAX (the count is halved until it fits). A page that is
        behind gets ``busy`` and asks again later."""
        if not _PANE_ID.fullmatch(pane) or count <= 0:
            return
        count = min(count, HISTORY_PAGE_MAX)
        if self.out.unacked() > LOW_WATER:
            self._send_history(pane, end, end, b"", None, busy=True)
            return
        while True:
            (head,) = await self.client.command(
                f"display -p -t {pane} '#{{history_added}}\t#{{history_collected}}'"
            )
            if not head.ok or not head.lines:
                return
            added, collected = (int(x) for x in head.text()[0].split("\t"))
            stop = min(end, added)
            start = max(stop - count, collected)
            if start >= stop:
                self._send_history(pane, start, start, b"", None)
                return
            info, lines = await self.client.command(
                f"display -p -t {pane} '#{{history_added}}\t#{{history_generation}}'",
                f"capture-pane -p -e -J -S -{added - start} -E -{added - stop + 1} -t {pane}",
            )
            if not info.ok or not lines.ok:
                return
            body = b"\r\n".join(lines.lines)
            if len(body) > HISTORY_BYTES_MAX and count > 1:
                count //= 2
                continue
            added_now, generation = (int(x) for x in info.text()[0].split("\t"))
            # Output may have scrolled more rows in between: the offsets were
            # relative to ``added``, so the rows captured moved by the
            # difference.
            shift = added_now - added
            self._send_history(pane, start + shift, stop + shift, body, generation)
            return

    def _send_history(
        self,
        pane: str,
        start: int,
        end: int,
        body: bytes,
        generation: int | None,
        *,
        busy: bool = False,
    ) -> None:
        header = {"pane": pane, "start": start, "end": end, "generation": generation}
        if busy:
            header["busy"] = True
        self.out.push(_Frame(_frame(FRAME_HISTORY, header, body)))


def _pending(reply: Reply) -> bytes:
    return b"\n".join(reply.lines) if reply.ok else b""


def _row(lines: list[bytes], row: int) -> bytes | None:
    return lines[row] if 0 <= row < len(lines) else None


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
