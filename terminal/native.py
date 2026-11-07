"""Native terminal: the ``/ws/terminal2`` WebSocket, a tmux control-mode bridge.

The classic terminal (``/ws/terminal``, ``terminal/routes.py``) runs a tmux
client on a PTY and streams the screen tmux draws. This bridge attaches a tmux
client in control mode instead (``terminal/control.py``): tmux draws nothing
and reports each pane's raw output, and the page parses it and keeps the
history itself, so it can scroll and select natively. Both terminals attach to
the same tmux server and can be open at once.

What the bridge guarantees (pinned by ``tests/unit/test_terminal_native.py``):

- **One pane on the wire.** The page shows the active pane of its client's
  current window. Only that pane's output is forwarded; the output of every
  other pane is dropped here. Nothing is ever paused in tmux: a pane paused or
  turned off by its only client stops being read, which would freeze the
  program writing to it (Phase 1 decision record).
- **Snapshots are exact.** Showing a pane starts with a snapshot: its modes, its
  recent history and its screen, captured in one command line, then the live
  stream. Output numbered up to the snapshot reply's ``seq`` is in the capture,
  output after it is not (``ControlClient`` orders both on one stream).
- **Bounded memory per page.** The page acknowledges the bytes it has parsed.
  Past ``HIGH_WATER`` unacknowledged bytes the bridge stops forwarding and drops
  the pane's output; once the page is back under ``LOW_WATER`` it gets a fresh
  snapshot. A page that stops reading never makes tmux, or this process, hold
  more.
- **Input exactly once.** Text goes as ``send-keys -H`` in 1 KiB commands, a
  paste through a private paste buffer with ``paste-buffer -p`` (tmux adds the
  bracketed-paste markers when the program asked for them), a named key as
  ``send-keys``: with ``-K`` when tmux binds it (the prefix, a root binding such
  as ``Escape`` or ``F2``, any key while a key table other than root is active),
  so the binding runs exactly as for a typed key; plainly otherwise, so tmux
  encodes it for the pane's own modes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from auth import verify_ws_cookie
from board import sweep as board_sweep
from terminal import routes as classic
from terminal.control import ControlClient, ControlError, Reply, hex_keys, quote
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

HIGH_WATER = 1024 * 1024
LOW_WATER = 256 * 1024
FRAME_MAX = 256 * 1024
SEND_TIMEOUT = 10.0
INPUT_CHUNK = 1024
INPUT_COMMANDS_PER_LINE = 8
PASTE_MAX = 1024 * 1024
INITIAL_HISTORY = 1000
HISTORY_PAGE_MAX = 2000
CLOSE_UNSUPPORTED = 4426

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

# What this client displays: the session frame's fields, then its pane.
_CLIENT_FMT = (
    "#{session_name}\t#{session_id}\t#{session_created}\t#{window_id}\t"
    "#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_width}\t#{pane_height}"
)
_WINDOW_FMT = (
    "#{window_id}\t#{window_index}\t#{window_name}\t#{window_active}\t#{@agent_state}"
)


# -- version ----------------------------------------------------------------------


def parse_tmux_version(text: str) -> tuple[int, int] | None:
    """``tmux 3.8`` -> (3, 8); ``tmux 3.7c`` -> (3, 7); ``tmux next-3.9`` ->
    (3, 9). A build without a number (``tmux master``) is newer than any
    release and reads as (99, 0). None when it is not tmux's answer."""
    text = text.strip()
    if not text.startswith("tmux "):
        return None
    match = _VERSION.search(text)
    if match is None:
        return (99, 0) if "master" in text else None
    return int(match.group(1)), int(match.group(2))


_version_cache: tuple[str, tuple[int, int] | None] | None = None


def tmux_version() -> tuple[str, tuple[int, int] | None]:
    """``tmux -V`` (no server needed), read once per process: the raw answer
    and its parsed version."""
    global _version_cache
    if _version_cache is None:
        raw = ""
        if shutil.which("tmux"):
            try:
                raw = subprocess.run(
                    ["tmux", "-V"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                raw = ""
        _version_cache = (raw, parse_tmux_version(raw))
    return _version_cache


def supported(version: tuple[int, int] | None) -> bool:
    return version is not None and version >= MIN_TMUX


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

    @property
    def pane(self) -> str:
        return self.values["pane_id"]

    @property
    def cols(self) -> int:
        return self.num("pane_width", 80)

    @property
    def rows(self) -> int:
        return self.num("pane_height", 24)


_CURSOR_STYLES = {"block": 1, "underline": 3, "bar": 5}


def _cup(row: int, col: int) -> bytes:
    return b"\x1b[%d;%dH" % (row + 1, col + 1)


def mode_sequences(modes: PaneModes) -> bytes:
    """Escape sequences that put a reset parser in the pane's modes. The cursor
    position comes last (DECSTBM and DECOM move it home)."""
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
    row = modes.num("cursor_y")
    if modes.flag("origin_flag"):
        row -= upper
    out += _cup(row, modes.num("cursor_x"))
    return bytes(out)


def build_replay(
    modes: PaneModes,
    lines: Sequence[bytes],
    alt_screen: Sequence[bytes] | None = None,
    pending: bytes = b"",
) -> bytes:
    """The bytes that rebuild a pane in a reset parser of its size.

    ``lines`` are the main screen's history and its visible rows, captured
    with ``-J`` (soft wraps joined, the parser wraps them again at the same
    width): written one per line, so the visible rows land on the screen and
    the rest scrolls into the parser's history. Each row is written whole: the
    cursor may sit at column 0 of a row already written (after a carriage
    return), so the cursor is placed afterwards, never by truncating.

    With the alternate screen on, ``alt_screen`` holds its rows (row-exact,
    no ``-J``): the main screen is written first with the saved cursor, the
    alternate screen entered, its rows drawn in place, then the modes.
    """
    out = bytearray(b"\r\n".join(line + b"\x1b[0m" for line in lines))
    if alt_screen is not None:
        out += _cup(modes.num("alternate_saved_y"), modes.num("alternate_saved_x"))
        out += b"\x1b[?1049h\x1b[H\x1b[2J"
        for row, line in enumerate(alt_screen[: modes.rows]):
            out += _cup(row, 0) + line + b"\x1b[0m"
    out += mode_sequences(modes)
    out += pending
    return bytes(out)


def _frame(kind: int, header: dict, body: bytes) -> bytes:
    return (
        bytes([kind])
        + json.dumps(header, separators=(",", ":")).encode()
        + b"\x00"
        + body
    )


# -- the bridge -------------------------------------------------------------------


@dataclass
class _Shown:
    """What the page displays: the client's session and window, and the
    active pane of that window with its size."""

    session: board_sweep.ClientSession
    pane: str
    cols: int
    rows: int


@dataclass
class _Outbox:
    """Frames waiting for the page, and the flow-control counters. Output
    frames are bytearrays so consecutive chunks coalesce in place."""

    frames: deque[bytes | bytearray] = field(default_factory=deque)
    queued: int = 0
    sent: int = 0
    acked: int = 0
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def unacked(self) -> int:
        return self.sent - self.acked + self.queued


class NativeBridge:
    """One page, one tmux control client."""

    def __init__(self, websocket: WebSocket, tmux: Sequence[str] = ("tmux",)) -> None:
        self.ws = websocket
        self.tmux = list(tmux)
        self.client = ControlClient(self._on_output, self._on_notification)
        self.report = classic.SessionReportState()
        self.out = _Outbox()
        self.shown: _Shown | None = None
        self.pane = ""
        self.windows_sent: list | None = None
        self.overflow = False
        self.snapshotting = False
        self.held: list[tuple[int, bytes]] = []
        self.dirty = asyncio.Event()
        self.prefixes: set[str] = set()
        self.closed = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._snapshot_lock = asyncio.Lock()
        self._input_lock = asyncio.Lock()
        self._resync: asyncio.Task | None = None

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
        env = terminal_process_env(os.environ, term="xterm-256color")
        await self.client.start(argv, env, cwd=cwd)
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
        self.report.websocket = self.ws
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
        if self._resync is not None:
            self._tasks.append(self._resync)
        for task in self._tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.close()
        self.closed.set()

    # -- tmux to page -------------------------------------------------------------

    def _on_output(self, pane: str, data: bytes) -> None:
        if pane != self.pane:
            return
        if self.snapshotting:
            self.held.append((self.client.seq, data))
            return
        if self.overflow:
            return
        self._enqueue_output(data)

    def _enqueue_output(self, data: bytes) -> None:
        if self.out.unacked() + len(data) > HIGH_WATER:
            # The page is behind: stop forwarding, drop until it catches up,
            # then resynchronise with a snapshot.
            self.overflow = True
            self._drop_queued_output()
            return
        prefix = bytes([FRAME_OUTPUT]) + self.pane.encode() + b"\x00"
        frames = self.out.frames
        last = frames[-1] if frames else None
        if (
            isinstance(last, bytearray)
            and last.startswith(prefix)
            and len(last) + len(data) <= FRAME_MAX
        ):
            last += data
        else:
            frames.append(bytearray(prefix + data))
        self.out.queued += len(data)
        self.out.wake.set()

    def _drop_queued_output(self) -> None:
        kept = deque(f for f in self.out.frames if f[:1] != b"\x01")
        self.out.frames = kept
        self.out.queued = sum(_payload_size(f) for f in kept)

    def _on_notification(self, name: str, rest: bytes) -> None:
        if name == "exit":
            self.closed.set()
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
        if name == "sessions-changed":
            self._control({"type": "sessions-changed"})

    def _control(self, payload: dict) -> None:
        self.out.frames.append(b"\x00" + json.dumps(payload).encode())
        self.out.wake.set()

    async def _sender(self) -> None:
        while True:
            await self.out.wake.wait()
            self.out.wake.clear()
            while self.out.frames:
                frame = self.out.frames.popleft()
                size = _payload_size(frame)
                self.out.queued -= size
                self.out.sent += size
                if frame[:1] == b"\x00":
                    send = self.ws.send_text("\x00" + bytes(frame[1:]).decode())
                else:
                    send = self.ws.send_bytes(bytes(frame))
                await asyncio.wait_for(send, SEND_TIMEOUT)

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
        await classic._send_session_if_changed(self.ws, shown.session, self.report)
        if windows.ok:
            rows = [_parse_window(line) for line in windows.text()]
            listing = [w for w in rows if w is not None]
            if listing != self.windows_sent:
                self.windows_sent = listing
                self._control(
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

    async def snapshot(self, pane: str) -> None:
        """Send ``pane``'s state, then its live stream from where the capture
        left off."""
        async with self._snapshot_lock:
            self.pane = pane
            self.snapshotting = True
            self.held = []
            try:
                replay, header, seq = await self._capture(pane)
            except ControlError:
                self.snapshotting = False
                raise
            if replay is None or self.pane != pane:
                self.snapshotting = False
                return
            # Everything queued for this pane is older than the capture.
            self._drop_queued_output()
            frame = _frame(FRAME_SNAPSHOT, header, replay)
            self.out.frames.append(frame)
            self.out.queued += len(replay)
            self.overflow = False
            held, self.held = self.held, []
            self.snapshotting = False
            for number, data in held:
                if number > seq and not self.overflow:
                    self._enqueue_output(data)
            self.out.wake.set()

    async def _capture(self, pane: str) -> tuple[bytes | None, dict, int]:
        t = f"-t {pane}"
        main = await self.client.command(
            f"display -p {t} {quote(_PANE_FMT)}",
            f"capture-pane -p -e -J -S -{INITIAL_HISTORY} -E - {t}",
            f"capture-pane -p -P {t}",
        )
        modes = (
            PaneModes.parse(main[0].text()[0]) if main[0].ok and main[0].lines else None
        )
        if modes is None or not all(r.ok for r in main):
            return None, {}, 0
        replies = main
        if modes.flag("alternate_on"):
            # The main screen is saved aside (-a); the visible rows are the
            # alternate screen's.
            alt = await self.client.command(
                f"display -p {t} {quote(_PANE_FMT)}",
                f"capture-pane -p -e -J -S -{INITIAL_HISTORY} -E -1 {t}",
                f"capture-pane -p -e -J -a {t}",
                f"capture-pane -p -e {t}",
                f"capture-pane -p -P {t}",
            )
            modes = (
                PaneModes.parse(alt[0].text()[0])
                if alt[0].ok and alt[0].lines
                else None
            )
            if modes is None or not all(r.ok for r in alt):
                # The program left the alternate screen in between: retry
                # the plain form.
                return await self._capture(pane) if modes is not None else (None, {}, 0)
            replies = alt
            lines = alt[1].lines + alt[2].lines
            replay = build_replay(modes, lines, alt[3].lines, _pending(alt[4]))
        else:
            replay = build_replay(modes, main[1].lines, None, _pending(main[2]))
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
        return replay, header, replies[0].seq

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
                self._control({"type": "pong"})
            elif kind == "clipboard_sync":
                classic._sync_clipboard(str(msg.get("text", "")))
        except (ValueError, TypeError):
            return
        except ControlError as e:
            logger.info("Native terminal command failed: %s", e)

    def ack(self, total: int) -> None:
        """The page has parsed ``total`` payload bytes since the socket
        opened."""
        if total > self.out.acked:
            self.out.acked = min(total, self.out.sent)
        if (
            self.overflow
            and not self.snapshotting
            and (self._resync is None or self._resync.done())
            and self.out.unacked() <= LOW_WATER
        ):
            self._resync = asyncio.create_task(self._resnapshot())

    async def _resnapshot(self) -> None:
        with contextlib.suppress(ControlError):
            await self.snapshot(self.pane)

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
            self._control(
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
        the pane's first line ever (``history_added`` counts them)."""
        if not _PANE_ID.fullmatch(pane) or count <= 0:
            return
        count = min(count, HISTORY_PAGE_MAX)
        (head,) = await self.client.command(
            f"display -p -t {pane} '#{{history_added}}\t#{{history_collected}}'"
        )
        if not head.ok or not head.lines:
            return
        added, collected = (int(x) for x in head.text()[0].split("\t"))
        end = min(end, added)
        start = max(end - count, collected)
        if start >= end:
            self._send_history(pane, start, start, b"", None)
            return
        info, lines = await self.client.command(
            f"display -p -t {pane} '#{{history_added}}\t#{{history_generation}}'",
            f"capture-pane -p -e -J -S -{added - start} -E -{added - end + 1} -t {pane}",
        )
        if not info.ok or not lines.ok:
            return
        added_now, generation = (int(x) for x in info.text()[0].split("\t"))
        # Output may have scrolled more rows in between: the offsets were
        # relative to ``added``, so the rows captured moved by the difference.
        shift = added_now - added
        self._send_history(
            pane, start + shift, end + shift, b"\r\n".join(lines.lines), generation
        )

    def _send_history(
        self, pane: str, start: int, end: int, body: bytes, generation: int | None
    ) -> None:
        header = {"pane": pane, "start": start, "end": end, "generation": generation}
        self.out.frames.append(_frame(FRAME_HISTORY, header, body))
        self.out.wake.set()


def _payload_size(frame: bytes | bytearray) -> int:
    """The bytes a frame counts against the flow-control window: pane output
    and snapshots, which the page acknowledges once parsed."""
    kind = frame[:1]
    if kind == b"\x01":
        return len(frame) - frame.index(b"\x00") - 1
    if kind == b"\x02":
        return len(frame) - frame.index(b"\x00") - 1
    return 0


def _pending(reply: Reply) -> bytes:
    return b"\n".join(reply.lines) if reply.ok else b""


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


async def native_ws(websocket: WebSocket) -> None:
    if not verify_ws_cookie(websocket):
        await websocket.close(code=4401, reason="Unauthorized")
        return
    await websocket.accept()
    raw, version = tmux_version()
    if not supported(version):
        await websocket.send_text(
            "\x00"
            + json.dumps(
                {
                    "type": "unsupported",
                    "version": raw,
                    "minimum": ".".join(map(str, MIN_TMUX)),
                }
            )
        )
        await websocket.close(code=CLOSE_UNSUPPORTED, reason="tmux too old")
        return
    preferred = parse_session_identity(
        websocket.query_params.get("session_id"),
        websocket.query_params.get("session_created"),
    )
    sessions = board_sweep.run_session_sweep_checked()
    if sessions is None:
        await websocket.close(code=1013, reason="tmux temporarily unavailable")
        return
    bridge = NativeBridge(websocket)
    try:
        await bridge.start(
            bridge.attach_argv(sessions, preferred),
            classic._cwd or str(classic.PROJECT_ROOT),
        )
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
