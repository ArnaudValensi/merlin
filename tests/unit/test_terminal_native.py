"""The native terminal bridge (``terminal/native.py``, ``/ws/terminal2``).

The real-tmux tests run a private server (``-S`` in ``tmp_path``, ``HOME`` in
``tmp_path`` so no personal ``~/.tmux.conf`` is read) with Merlin's own
``terminal/tmux.conf``, and drive a ``NativeBridge`` through a fake WebSocket.
Nothing reaches the user's tmux server.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from terminal import native
from terminal import routes as classic
from terminal.control import quote
from terminal.native import (
    FRAME_HISTORY,
    FRAME_OUTPUT,
    FRAME_SNAPSHOT,
    HIGH_WATER,
    NativeBridge,
    PaneModes,
    build_replay,
    mode_sequences,
    parse_tmux_version,
    supported,
)
from terminal.tmux import TMUX_CONF

HAS_TMUX = shutil.which("tmux") is not None
needs_tmux = pytest.mark.skipif(not HAS_TMUX, reason="tmux not installed")
MERLIN_CLIP = Path(__file__).resolve().parents[2] / "bin" / "merlin-clip"


# -- a fake page --------------------------------------------------------------------


class FakeWS:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[str | bytes] = []
        self.headers: dict[str, str] = {
            "user-agent": "Mozilla/5.0 (X11; Linux) Firefox/150"
        }
        self.query_params: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.closed: tuple[int, str] | None = None

    async def accept(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def receive(self) -> dict:
        return await self.incoming.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    def push(self, msg: dict) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(msg)})

    def controls(self, kind: str | None = None) -> list[dict]:
        out = [
            json.loads(m[1:])
            for m in self.sent
            if isinstance(m, str) and m[:1] == "\x00"
        ]
        return [c for c in out if kind is None or c.get("type") == kind]

    def binaries(self) -> list[tuple[int, dict | str, bytes]]:
        out = []
        for m in self.sent:
            if not isinstance(m, bytes):
                continue
            head, _, body = m[1:].partition(b"\x00")
            if m[0] == FRAME_OUTPUT:
                out.append((m[0], head.decode(), body))
            else:
                out.append((m[0], json.loads(head), body))
        return out

    def stream_after_last_snapshot(self) -> tuple[dict, bytes]:
        """The last snapshot's header and the bytes a page would parse from it
        on: the replay, then every output frame after it."""
        frames = self.binaries()
        last = max(i for i, f in enumerate(frames) if f[0] == FRAME_SNAPSHOT)
        header = frames[last][1]
        assert isinstance(header, dict)
        data = frames[last][2] + b"".join(
            body for kind, pane, body in frames[last + 1 :] if kind == FRAME_OUTPUT
        )
        return header, data


class MiniScreen:
    """Just enough of a terminal to read text back: printable characters,
    CR, LF (scrolling into history), CUP; other sequences are skipped."""

    _CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

    def __init__(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.lines: list[list[str]] = [[] for _ in range(rows)]
        self.top = self.cy = self.cx = 0

    def feed(self, text: str) -> MiniScreen:
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\x1b":
                m = self._CSI.match(text, i)
                if m:
                    seq = m.group(0)
                    if seq.endswith("H") and "?" not in seq:
                        parts = (seq[2:-1].split(";") + ["", ""])[:2]
                        self.cy = max(int(parts[0] or 1), 1) - 1
                        self.cx = max(int(parts[1] or 1), 1) - 1
                    i = m.end()
                    continue
                i += 2
                continue
            if ch == "\r":
                self.cx = 0
            elif ch == "\n":
                self._down()
            elif ch >= " ":
                if self.cx >= self.cols:
                    self.cx = 0
                    self._down()
                row = self.lines[self.top + self.cy]
                row.extend(" " * (self.cx + 1 - len(row)))
                row[self.cx] = ch
                self.cx += 1
            i += 1
        return self

    def _down(self) -> None:
        self.cy += 1
        if self.cy == self.rows:
            self.lines.append([])
            self.top += 1
            self.cy = self.rows - 1

    def text(self) -> list[str]:
        return ["".join(line).rstrip() for line in self.lines]


# -- pure ---------------------------------------------------------------------------


def test_parse_tmux_version():
    assert parse_tmux_version("tmux 3.8") == (3, 8)
    assert parse_tmux_version("tmux 3.7c\n") == (3, 7)
    assert parse_tmux_version("tmux next-3.9") == (3, 9)
    assert parse_tmux_version("tmux master") == (99, 0)
    assert parse_tmux_version("") is None
    assert parse_tmux_version("bash: tmux: not found") is None
    assert supported((3, 8)) and supported((4, 0))
    assert not supported((3, 7)) and not supported((3, 4)) and not supported(None)


def _modes(**over: str) -> PaneModes:
    values = dict.fromkeys(native._PANE_FIELDS, "0")
    values.update(
        pane_id="%1",
        pane_width="80",
        pane_height="24",
        cursor_flag="1",
        wrap_flag="1",
        scroll_region_lower="23",
        cursor_shape="default",
    )
    values.update(over)
    return PaneModes(values)


def test_mode_sequences_restore_every_mode_and_place_the_cursor_last():
    seq = mode_sequences(
        _modes(
            keypad_cursor_flag="1",
            keypad_flag="1",
            bracket_paste_flag="1",
            mouse_button_flag="1",
            mouse_sgr_flag="1",
            insert_flag="1",
            wrap_flag="0",
            cursor_flag="0",
            cursor_shape="bar",
            cursor_blinking="1",
            scroll_region_upper="2",
            scroll_region_lower="20",
            cursor_x="4",
            cursor_y="7",
        )
    )
    for part in (
        b"\x1b[3;21r",
        b"\x1b[?7l",
        b"\x1b[4h",
        b"\x1b[?1h",
        b"\x1b=",
        b"\x1b[?1002h",
        b"\x1b[?1006h",
        b"\x1b[?2004h",
        b"\x1b[5 q",
        b"\x1b[?25l",
    ):
        assert part in seq
    assert seq.endswith(b"\x1b[8;5H")


def test_mode_sequences_default_pane_is_just_the_cursor():
    assert mode_sequences(_modes(cursor_x="2", cursor_y="0")) == b"\x1b[1;3H"


def test_origin_mode_cursor_is_relative_to_the_region():
    seq = mode_sequences(
        _modes(
            origin_flag="1",
            scroll_region_upper="5",
            scroll_region_lower="10",
            cursor_y="7",
        )
    )
    assert seq.endswith(b"\x1b[3;1H")


def test_build_replay_keeps_rows_whole_and_appends_pending_bytes():
    replay = build_replay(
        _modes(cursor_x="0", cursor_y="1"), [b"one", b"two"], None, b"\x1b[3"
    )
    assert replay == b"one\x1b[0m\r\ntwo\x1b[0m\x1b[2;1H\x1b[3"


def test_build_replay_alternate_screen_draws_main_then_enters_alternate():
    replay = build_replay(
        _modes(
            alternate_on="1", alternate_saved_x="3", alternate_saved_y="1", cursor_y="0"
        ),
        [b"main"],
        [b"vim", b"~"],
    )
    assert replay.startswith(b"main\x1b[0m\x1b[2;4H\x1b[?1049h\x1b[H\x1b[2J")
    assert b"\x1b[1;1Hvim\x1b[0m\x1b[2;1H~\x1b[0m" in replay


def test_parse_shown_and_window():
    shown = native._parse_shown("t\t$0\t1700000000\t@1\t0\tsh\t%3\t80\t24")
    assert shown is not None and shown.pane == "%3" and shown.session.name == "t"
    assert native._parse_shown("garbage") is None
    assert native._parse_window("@1\t0\tsh\t1\tbusy") == {
        "id": "@1",
        "index": 0,
        "name": "sh",
        "active": True,
        "state": "busy",
    }


@pytest.mark.asyncio
async def test_a_page_that_stops_acking_overflows_then_resyncs_on_catch_up():
    ws = FakeWS()
    bridge = NativeBridge(ws)  # type: ignore[arg-type]
    bridge.pane = "%1"
    chunk = b"x" * 65536
    for _ in range(40):  # 2.5 MiB, never acknowledged
        bridge._on_output("%1", chunk)
    assert bridge.overflow
    assert bridge.out.unacked() <= HIGH_WATER
    # Other panes never reach the page.
    queued = bridge.out.queued
    bridge._on_output("%2", chunk)
    assert bridge.out.queued == queued
    # The page catches up: a snapshot is scheduled (it fails here, there is
    # no tmux, and the failure is contained).
    bridge.out.sent, bridge.out.queued, bridge.out.frames = (
        queued,
        0,
        type(bridge.out.frames)(),
    )
    bridge.ack(queued)
    assert bridge._resync is not None
    await bridge._resync


# -- real tmux ------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    socket = str(tmp_path / "s")
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env.update(HOME=str(tmp_path), SHELL="/bin/sh")

    def tmux(*args: str) -> str:
        return subprocess.run(
            ["tmux", "-S", socket, *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        ).stdout

    tmux(
        "-f",
        str(TMUX_CONF),
        "new-session",
        "-d",
        "-s",
        "t",
        "-x",
        "80",
        "-y",
        "24",
        "/bin/sh",
    )
    try:
        yield socket, tmux, tmp_path
    finally:
        subprocess.run(
            ["tmux", "-S", socket, "kill-server"],
            capture_output=True,
            env=env,
            check=False,
        )


async def _until(pred, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def _bridge(
    socket: str, session: str = "t"
) -> tuple[NativeBridge, FakeWS, asyncio.Task]:
    ws = FakeWS()
    bridge = NativeBridge(ws, tmux=("tmux", "-S", socket))  # type: ignore[arg-type]
    await bridge.start(
        ["tmux", "-S", socket, "-C", "attach", "-t", session], str(Path.cwd())
    )
    task = asyncio.create_task(bridge.run())
    assert await _until(lambda: any(f[0] == FRAME_SNAPSHOT for f in ws.binaries()))
    return bridge, ws, task


async def _stop(bridge: NativeBridge, task: asyncio.Task) -> None:
    await bridge.close()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _output(ws: FakeWS) -> bytes:
    return b"".join(body for kind, _, body in ws.binaries() if kind == FRAME_OUTPUT)


async def _run(bridge: NativeBridge, command: str) -> None:
    await bridge.send_input(bridge.pane, command + "\r")


@needs_tmux
@pytest.mark.asyncio
async def test_attach_reports_session_windows_and_a_snapshot(server):
    socket, tmux, _ = server
    bridge, ws, task = await _bridge(socket)
    try:
        session = ws.controls("session")[-1]
        assert session["name"] == "t" and session["window_id"].startswith("@")
        windows = ws.controls("windows")[-1]["windows"]
        assert [w["active"] for w in windows] == [True]
        header = next(f[1] for f in ws.binaries() if f[0] == FRAME_SNAPSHOT)
        assert isinstance(header, dict)
        assert (
            header["pane"] == bridge.pane
            and header["cols"] == 80
            and header["rows"] == 24
        )
        assert {"added", "collected", "generation", "size"} <= set(header["history"])
        # The page counts as looking at its window.
        assert classic.looking_at(f"t:{session['window_id']}")
        rows = tmux(
            "list-clients", "-F", "#{client_control_mode}|#{client_tty}"
        ).split()
        assert rows == ["1|"]
    finally:
        await _stop(bridge, task)
    assert not classic.looking_at(f"t:{session['window_id']}")
    assert tmux("ls", "-F", "#{session_name}").split() == ["t"]
    assert tmux("list-clients").strip() == ""


@needs_tmux
@pytest.mark.asyncio
async def test_snapshot_mid_flood_then_live_stream_is_exact(server):
    """The Reply.seq boundary with a real consumer: snapshot replay plus the
    output frames after it rebuild every line once."""
    socket, _, _ = server
    bridge, ws, task = await _bridge(socket)
    try:
        n = 60_000
        await _run(bridge, f"clear; seq 1 {n}; echo DONE-$((40+2))")
        assert await _until(lambda: len(_output(ws)) > 30_000)
        await bridge.snapshot(bridge.pane)
        assert await _until(lambda: b"DONE-42" in ws.stream_after_last_snapshot()[1])
        header, data = ws.stream_after_last_snapshot()
        screen = MiniScreen(header["cols"], header["rows"]).feed(data.decode())
        numbers = [line for line in screen.text() if line.isdigit()]
        # The snapshot carries the last INITIAL_HISTORY rows of history, so
        # the run starts there and must be gapless and single to the end.
        first = int(numbers[0])
        assert numbers == [str(i) for i in range(first, n + 1)]
        assert first <= n - native.INITIAL_HISTORY
    finally:
        await _stop(bridge, task)


@needs_tmux
@pytest.mark.asyncio
async def test_input_reaches_the_pane_exactly_once(server):
    socket, _, tmp = server
    bridge, _, task = await _bridge(socket)
    try:
        payload = (
            "Enter C-c ; \\ 'q' \"d\" #{pane_id} é 日本 🎉 \x1b[A\t\x01\x03\x7f"
            + "z" * 3000
        )
        out = tmp / "typed"
        size = len(payload.encode())
        await _run(bridge, f"stty raw -echo; head -c {size} > {out}; stty sane")
        await asyncio.sleep(0.3)
        await bridge.send_input(bridge.pane, payload)
        assert await _until(lambda: out.exists() and out.stat().st_size >= size)
        await asyncio.sleep(0.1)
        assert out.read_bytes() == payload.encode()
    finally:
        await _stop(bridge, task)


@needs_tmux
@pytest.mark.asyncio
async def test_paste_is_exact_and_bracketed_when_the_program_asked(server):
    socket, _, tmp = server
    bridge, _, task = await _bridge(socket)
    try:
        text = "line one\rline 'two'; $HOME é\r" + "p" * 200_000
        size = len(text.encode()) + 12  # the two bracketed-paste markers
        out = tmp / "pasted"
        await _run(
            bridge,
            f"printf '\\033[?2004h'; stty raw -echo; head -c {size} > {out}; stty sane",
        )
        await asyncio.sleep(0.3)
        tmpdir = Path(tempfile.gettempdir())
        before = set(tmpdir.glob("merlin-paste-*"))
        await bridge.paste(bridge.pane, text)
        # The paste file is gone once tmux has loaded it.
        assert set(tmpdir.glob("merlin-paste-*")) <= before
        assert await _until(lambda: out.exists() and out.stat().st_size >= size)
        assert out.read_bytes() == b"\x1b[200~" + text.encode() + b"\x1b[201~"
    finally:
        await _stop(bridge, task)


@needs_tmux
@pytest.mark.asyncio
async def test_bound_keys_run_their_binding_and_unbound_keys_follow_the_pane_modes(
    server,
):
    socket, tmux, tmp = server
    bridge, ws, task = await _bridge(socket)
    try:
        window = ws.controls("session")[-1]["window_id"]
        # Escape on a busy agent window: tmux.conf's binding marks it idle and
        # still sends the key to the pane.
        tmux("set", "-w", "-t", window, "@agent_state", "busy")
        out = tmp / "esc"
        await _run(bridge, f"stty raw -echo; head -c 4 > {out}; stty sane")
        await asyncio.sleep(0.3)
        await bridge.send_key(bridge.pane, "Escape")
        assert await _until(
            lambda: tmux("show", "-wv", "-t", window, "@agent_state").strip() == "idle"
        )
        # Up in application cursor mode is encoded by tmux for the pane.
        await bridge.send_key(bridge.pane, "Up")
        assert await _until(lambda: out.exists() and out.stat().st_size >= 4)
        assert out.read_bytes() == b"\x1b\x1b[A"
        out2 = tmp / "app"
        await _run(
            bridge, f"printf '\\033[?1h'; stty raw -echo; head -c 3 > {out2}; stty sane"
        )
        await asyncio.sleep(0.3)
        await bridge.send_key(bridge.pane, "Up")
        assert await _until(lambda: out2.exists() and out2.stat().st_size >= 3)
        assert out2.read_bytes() == b"\x1bOA"
        # F2 is bound in tmux.conf: a new window, reported to the page.
        await bridge.send_key(bridge.pane, "F2")
        assert await _until(lambda: len(ws.controls("windows")[-1]["windows"]) == 2)
        # Malformed names never reach tmux.
        await bridge.send_key(bridge.pane, "Up ; kill-server")
        await bridge.send_key("not-a-pane", "Up")
        assert tmux("ls", "-F", "#{session_name}").split() == ["t"]
    finally:
        await _stop(bridge, task)


@needs_tmux
@pytest.mark.asyncio
async def test_resize_switch_and_window_events_reach_the_page(server):
    socket, tmux, _ = server
    bridge, ws, task = await _bridge(socket)
    try:
        ws.push({"type": "resize", "cols": 100, "rows": 30})
        assert await _until(
            lambda: any(
                f[0] == FRAME_SNAPSHOT
                and isinstance(f[1], dict)
                and f[1]["cols"] == 100
                for f in ws.binaries()
            )
        )
        tmux("new-session", "-d", "-s", "other", "/bin/sh")
        tmux("new-window", "-d", "-t", "other:", "-n", "second", "/bin/sh")
        ws.push({"type": "switch", "target": "other"})
        assert await _until(lambda: ws.controls("session")[-1]["name"] == "other")
        assert await _until(
            lambda: (
                [w["name"] for w in ws.controls("windows")[-1]["windows"]][-1]
                == "second"
            )
        )
        second = ws.controls("windows")[-1]["windows"][-1]["id"]
        ws.push({"type": "switch", "target": f"other:{second}"})
        assert await _until(lambda: ws.controls("session")[-1]["window_id"] == second)
        tmux("rename-window", "-t", second, "renamed")
        assert await _until(
            lambda: ws.controls("windows")[-1]["windows"][-1]["name"] == "renamed"
        )
        tmux("set", "-w", "-t", second, "@agent_state", "ask")
        assert await _until(
            lambda: ws.controls("windows")[-1]["windows"][-1]["state"] == "ask"
        )
    finally:
        await _stop(bridge, task)


@needs_tmux
@pytest.mark.asyncio
async def test_a_page_that_falls_behind_is_dropped_then_resynchronised(server):
    socket, _, _ = server
    bridge, ws, task = await _bridge(socket)
    try:
        snapshots = len([f for f in ws.binaries() if f[0] == FRAME_SNAPSHOT])
        await _run(bridge, "clear; yes | head -c 4000000; echo; echo DONE-$((40+3))")
        assert await _until(lambda: bridge.overflow)
        # Never acknowledged: what was sent is bounded by the window.
        await asyncio.sleep(0.5)
        assert bridge.out.unacked() <= HIGH_WATER
        assert len(_output(ws)) <= HIGH_WATER
        # The page catches up: one fresh snapshot, then the live stream.
        await asyncio.sleep(1.0)
        bridge.ack(bridge.out.sent)
        assert await _until(
            lambda: (
                len([f for f in ws.binaries() if f[0] == FRAME_SNAPSHOT]) > snapshots
            )
        )
        assert await _until(lambda: b"DONE-43" in ws.stream_after_last_snapshot()[1])
        header, data = ws.stream_after_last_snapshot()
        screen = MiniScreen(header["cols"], header["rows"]).feed(data.decode())
        assert "DONE-43" in screen.text()
    finally:
        await _stop(bridge, task)


@needs_tmux
@pytest.mark.asyncio
async def test_osc52_from_merlin_clip_rides_the_pane_output(server):
    """A control client has no tty, so merlin-clip writes its OSC 52 into the
    pane, where the page's parser takes it: nothing is written into the
    control stream itself."""
    socket, _, _ = server
    bridge, ws, task = await _bridge(socket)
    try:
        await _run(bridge, f"printf copied | sh {quote(str(MERLIN_CLIP))} copy")
        encoded = base64.b64encode(b"copied")
        assert await _until(lambda: b"\x1b]52;c;" + encoded in _output(ws))
    finally:
        await _stop(bridge, task)


@needs_tmux
@pytest.mark.asyncio
async def test_history_rows_by_absolute_number(server):
    socket, tmux, _ = server
    bridge, ws, task = await _bridge(socket)
    try:
        await _run(bridge, "clear; seq 1 3000")
        assert await _until(lambda: b"3000" in _output(ws))
        await asyncio.sleep(0.2)
        added = int(
            tmux("display", "-p", "-t", bridge.pane, "#{history_added}").strip()
        )
        ws.push(
            {"type": "history", "pane": bridge.pane, "end": added - 100, "count": 50}
        )
        assert await _until(lambda: any(f[0] == FRAME_HISTORY for f in ws.binaries()))
        header, body = next(
            (f[1], f[2]) for f in ws.binaries() if f[0] == FRAME_HISTORY
        )
        assert isinstance(header, dict)
        assert header["end"] - header["start"] == 50
        lines = body.decode().split("\r\n")
        assert len(lines) == 50
        # Row number k (from the pane's first line) is the line "k + c" for a
        # constant c: consecutive, and the requested range.
        assert [int(x) for x in lines] == list(range(int(lines[0]), int(lines[0]) + 50))
    finally:
        await _stop(bridge, task)


@pytest.mark.asyncio
async def test_endpoint_refuses_an_old_tmux_with_a_notice(monkeypatch):
    monkeypatch.setattr(native, "verify_ws_cookie", lambda ws: True)
    monkeypatch.setattr(native, "tmux_version", lambda: ("tmux 3.4", (3, 4)))
    ws = FakeWS()
    await native.native_ws(ws)  # type: ignore[arg-type]
    notice = ws.controls("unsupported")
    assert notice == [{"type": "unsupported", "version": "tmux 3.4", "minimum": "3.8"}]
    assert ws.closed is not None and ws.closed[0] == native.CLOSE_UNSUPPORTED


@pytest.mark.asyncio
async def test_endpoint_rejects_without_a_cookie(monkeypatch):
    monkeypatch.setattr(native, "verify_ws_cookie", lambda ws: False)
    ws = FakeWS()
    await native.native_ws(ws)  # type: ignore[arg-type]
    assert ws.closed == (4401, "Unauthorized")


def test_attach_ladder_is_the_classic_one_in_control_mode(monkeypatch):
    from board.sweep import TmuxSession
    from terminal.tmux import SessionIdentity, tmux_conf_args

    monkeypatch.setattr(native, "server_spawn_prefix", lambda running: [])
    bridge = NativeBridge(FakeWS())  # type: ignore[arg-type]
    conf = tmux_conf_args()
    row = TmuxSession("dev", "$3", 1700000000, False, 1, 0)
    assert bridge.attach_argv([row], SessionIdentity("$3", 1700000000)) == [
        "tmux",
        "-C",
        *conf,
        "attach",
        "-t",
        "$3",
    ]
    assert bridge.attach_argv([row], SessionIdentity("$3", 1)) == [
        "tmux",
        "-C",
        *conf,
        "attach",
    ]
    assert bridge.attach_argv([], None) == [
        "tmux",
        "-C",
        *conf,
        "new-session",
        "-A",
        "-s",
        "merlin-dev",
        "-x",
        "120",
        "-y",
        "40",
    ]


@pytest.mark.asyncio
async def test_the_control_client_gets_the_terminal_environment(monkeypatch):
    """The client may create the server, which every shell inherits: the same
    environment rules as the classic terminal (no credentials, the hooks
    directory, TERM)."""
    monkeypatch.setenv("MERLIN_SAAS_TOKEN", "secret")
    monkeypatch.setenv("TMUX", "/tmp/elsewhere,1,0")
    seen: dict = {}

    async def fake_start(argv, env, cwd=None):
        seen.update(env=env, cwd=cwd)
        raise native.ControlError("stop here")

    bridge = NativeBridge(FakeWS())  # type: ignore[arg-type]
    monkeypatch.setattr(bridge.client, "start", fake_start)
    with pytest.raises(native.ControlError):
        await bridge.start(["tmux", "-C", "attach"], "/somewhere")
    env = seen["env"]
    assert "MERLIN_SAAS_TOKEN" not in env and "TMUX" not in env
    assert env["TERM"] == "xterm-256color" and env["MERLIN_TERMINAL_HOOKS"]
    assert seen["cwd"] == "/somewhere"
