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
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from terminal import native
from terminal import routes as classic
from terminal.control import ControlClient, quote
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
needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)
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


# -- pure ---------------------------------------------------------------------------


def test_parse_tmux_version():
    assert parse_tmux_version("tmux 3.8") == (3, 8)
    assert parse_tmux_version("3.8") == (3, 8)  # the #{version} format
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
    # Region and origin first (they home the cursor), the cursor next, the
    # insert mode only after it.
    assert seq.index(b"\x1b[3;21r") < seq.index(b"\x1b[8;5H") < seq.index(b"\x1b[4h")


def test_pending_wrap_rewrites_the_cursor_row():
    seq = mode_sequences(_modes(pane_width="5", cursor_x="5", cursor_y="2"), b"ABCDE")
    assert seq == b"\x1b[3;1HABCDE"
    # Without the row, the cursor is clamped to the last column.
    assert (
        mode_sequences(_modes(pane_width="5", cursor_x="5", cursor_y="2"))
        == b"\x1b[3;5H"
    )


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
    assert b"\x1b[3;1H" in seq


def test_build_replay_keeps_rows_whole():
    replay = build_replay(_modes(cursor_x="0", cursor_y="1"), [b"one", b"two"])
    # G1 is DEC graphics for the SO/SI around captured line-drawing cells.
    assert replay == b"\x1b)0one\x1b[0m\r\ntwo\x1b[0m\x1b[2;1H"


def test_build_replay_alternate_screen_draws_main_then_enters_alternate():
    replay = build_replay(
        _modes(
            alternate_on="1", alternate_saved_x="3", alternate_saved_y="1", cursor_y="0"
        ),
        [b"main"],
        [b"vim", b"~"],
    )
    assert replay.startswith(b"\x1b)0main\x1b[0m\x1b[2;4H\x1b[?1049h\x1b[H\x1b[2J")
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
@needs_node
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
        lines = _xterm_states(header["cols"], header["rows"], {"page": [data]})["page"][
            "text"
        ]
        numbers = [line for line in lines if line.isdigit()]
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
@needs_node
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
        lines = _xterm_states(header["cols"], header["rows"], {"page": [data]})["page"][
            "text"
        ]
        assert "DONE-43" in lines
    finally:
        await _stop(bridge, task)


class ClassicClient:
    """A normal tmux client on a PTY, as the classic terminal runs one; what
    reaches its tty is read from the PTY master."""

    def __init__(self, socket: str, session: str) -> None:
        import pty

        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["TERM"] = "xterm-256color"
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # pragma: no cover - the child only execs
            try:
                os.execvpe("tmux", ["tmux", "-S", socket, "attach", "-t", session], env)
            finally:
                os._exit(1)
        os.set_blocking(self.fd, False)
        self.received = bytearray()

    def read(self) -> bytes:
        with contextlib.suppress(BlockingIOError, OSError):
            while True:
                chunk = os.read(self.fd, 65536)
                if not chunk:
                    break
                self.received += chunk
        return bytes(self.received)

    def close(self) -> None:
        import signal

        with contextlib.suppress(OSError):
            os.kill(self.pid, signal.SIGHUP)
        with contextlib.suppress(OSError):
            os.close(self.fd)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(self.pid, 0)


def _osc52(text: str) -> bytes:
    return b"\x1b]52;c;" + base64.b64encode(text.encode())


@needs_tmux
@pytest.mark.asyncio
@pytest.mark.parametrize("classic_first", [True, False])
async def test_merlin_clip_reaches_both_terminals_when_they_coexist(
    server, classic_first
):
    """With a classic client and a native page on one session, whichever
    client tmux calls current: the classic tty gets its OSC 52, the native page
    a clipboard frame, and nothing is written into the pane (or a control
    stream). Both the shell path and a provider path (stdout not the pane, as
    when NeoVim runs merlin-clip) behave the same."""
    socket, tmux, tmp = server
    classic_client = None
    if classic_first:
        classic_client = ClassicClient(socket, "t")
        assert await _until(lambda: tmux("list-clients").strip() != "")
    bridge, ws, task = await _bridge(socket)
    try:
        if classic_client is None:
            classic_client = ClassicClient(socket, "t")
            assert await _until(lambda: len(tmux("list-clients").split("\n")) >= 3)
        clip = quote(str(MERLIN_CLIP))
        await _run(bridge, f"printf shell-copy | sh {clip} copy")
        assert await _until(lambda: _osc52("shell-copy") in classic_client.read())
        assert await _until(
            lambda: any(c.get("text") == "shell-copy" for c in ws.controls("clipboard"))
        )
        out = tmp / "provider-stdout"
        await _run(bridge, f"printf provider-copy | sh {clip} copy > {out}")
        assert await _until(lambda: _osc52("provider-copy") in classic_client.read())
        assert await _until(
            lambda: any(
                c.get("text") == "provider-copy" for c in ws.controls("clipboard")
            )
        )
        assert out.read_bytes() == b""
        assert b"\x1b]52" not in _output(ws)
    finally:
        classic_client.close()
        await _stop(bridge, task)


@needs_tmux
@pytest.mark.asyncio
async def test_merlin_clip_with_only_a_native_page(server):
    socket, _, _ = server
    bridge, ws, task = await _bridge(socket)
    try:
        await _run(
            bridge, f"printf 'two\\nlines\\n' | sh {quote(str(MERLIN_CLIP))} copy"
        )
        assert await _until(
            lambda: any(
                c.get("text") == "two\nlines\n" for c in ws.controls("clipboard")
            )
        )
        assert b"\x1b]52" not in _output(ws)
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


# -- differential: snapshot + live stream against the uninterrupted stream -----------

XTERM_STATE = Path(__file__).with_name("xterm_state.js")
needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def _xterm_states(cols: int, rows: int, streams: dict[str, list[bytes]]) -> dict:
    """The page's own parser (vendored xterm.js, never opened) after each
    stream, run by node."""
    request = {
        "cols": cols,
        "rows": rows,
        "streams": {
            name: [base64.b64encode(c).decode() for c in chunks]
            for name, chunks in streams.items()
        },
    }
    done = subprocess.run(
        ["node", str(XTERM_STATE)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(done.stdout)


def _state_diff(got: dict, want: dict) -> list[str]:
    """Readable differences between two parser states."""
    out = []
    for key in ("type", "cursor", "modes", "cursorHidden", "recentHistory"):
        if got[key] != want[key]:
            out.append(f"{key}: {got[key]!r} != {want[key]!r}")
    for screen in ("active", "normal"):
        for y, (g, w) in enumerate(zip(got[screen], want[screen], strict=True)):
            if g != w:
                text = lambda row: "".join(c[0] or " " for c in row["cells"])  # noqa: E731
                attrs = [
                    x
                    for x, (a, b) in enumerate(zip(g["cells"], w["cells"], strict=True))
                    if a != b
                ]
                out.append(
                    f"{screen}[{y}]: {text(g)!r} != {text(w)!r} wrapped {g['wrapped']}/{w['wrapped']} cells {attrs[:5]}"
                )
    return out


# Each case: what the program writes before the snapshot, then after it.
DIFFERENTIAL = {
    "rendition": (r"\033[31;1;4mA", r"B\033[0mC"),
    "rendition-256-and-rgb": (r"\033[38;5;208;48;2;1;2;3mA", r"B"),
    "pending-wrap": (r"%020d", r"F"),
    "partial-escape": (r"x\033[3", r"1mR"),
    "split-utf8": (r"a\303", r"\251b"),
    "alternate-enter": (r"main\r\n\033[?1049h\033[Halt", r"\033[?1049lback"),
    "alternate-leave": (r"one\r\n\033[?1049h\033[Hfull", r"!\033[?1049ltwo"),
    "charset": (r"\033(0lqk", r"x\033(Bq"),
    "shift-out": (r"\033)0\016lq", r"k\017q"),
    "modes": (
        r"\033[?1h\033=\033[?2004h\033[?1002h\033[?1006h\033[?25l\033[4h\033[2;5r",
        r"Z",
    ),
    "origin": (r"\033[2;4r\033[?6h\033[2;3Hin", r"\033[1;1Hz"),
    "no-wrap": (r"\033[?7l%025d", r"Q"),
}


@needs_tmux
@needs_node
@pytest.mark.asyncio
async def test_snapshot_then_live_equals_the_uninterrupted_stream(server):
    """A snapshot taken while the program pauses, followed by the live bytes,
    leaves the page's parser exactly where the whole stream does: cells,
    attributes, wraps, cursor (pending wrap included), modes, both screens.
    Every case runs at once, each in its own session with its own bridge and
    a recorder that sees the pane from its first byte."""
    socket, tmux, tmp = server
    cols, rows = 20, 6
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}

    async def one(index: int, case: str) -> tuple[str, list[str]]:
        before, after = DIFFERENTIAL[case]
        session = f"d{index}"
        script = tmp / f"{session}.sh"
        script.write_text(
            f"sleep 0.8; printf '{before}'; sleep 1.4; printf '{after}'; sleep 30\n"
        )
        tmux(
            "new-session",
            "-d",
            "-s",
            session,
            "-x",
            str(cols),
            "-y",
            str(rows),
            f"sh {script}",
        )
        recorded: dict[str, bytearray] = {}
        recorder = ControlClient(
            lambda pane, data: recorded.setdefault(pane, bytearray()).extend(data),
            lambda name, rest: None,
        )
        await recorder.start(["tmux", "-S", socket, "-C", "attach", "-t", session], env)
        bridge, ws, task = await _bridge(socket, session)
        try:
            pane = bridge.pane
            await asyncio.sleep(1.4)  # inside the program's pause
            await bridge.snapshot(pane)
            await asyncio.sleep(1.6)  # the second write has happened
            _, got = ws.stream_after_last_snapshot()
            reference = bytes(recorded.get(pane, b""))
            if not reference:
                return case, ["the recorder saw nothing"]
            states = _xterm_states(
                cols, rows, {"reference": [reference], "replayed": [got]}
            )
            return case, _state_diff(states["replayed"], states["reference"])
        finally:
            await _stop(bridge, task)
            await recorder.close()

    results = await asyncio.gather(
        *(one(i, case) for i, case in enumerate(sorted(DIFFERENTIAL)))
    )
    assert {case: diff for case, diff in results if diff} == {}


# -- flow control and delivery, in memory --------------------------------------------


class FakeControl:
    """Stands in for the control client: answers the snapshot's commands with a
    one-row pane, numbers output like the real one."""

    def __init__(self) -> None:
        self.seq = 0
        self.calls: list[tuple[str, ...]] = []
        self.gate: asyncio.Event | None = None
        self.closed = False
        self.exited = asyncio.Event()
        self.pid = None

    async def command(self, *commands: str, timeout: float = 15.0):
        from terminal.control import Reply

        self.calls.append(commands)
        if self.gate is not None:
            await self.gate.wait()
        replies = []
        for c in commands:
            if (
                c.startswith("display -p -t")
                and "pane_width" not in c
                and "#{pane_id}" not in c
            ):
                replies.append(Reply(True, [b"100\t0"], self.seq))
            elif "pane_id" in c and "pane_width" in c:
                values = dict.fromkeys(native._PANE_FIELDS, "0")
                values.update(
                    pane_id="%1",
                    pane_width="10",
                    pane_height="2",
                    cursor_flag="1",
                    wrap_flag="1",
                    scroll_region_lower="1",
                    cursor_shape="default",
                    bracket_paste_flag="0",
                    history_added="0",
                    history_collected="0",
                )
                replies.append(
                    Reply(
                        True,
                        ["\t".join(values[f] for f in native._PANE_FIELDS).encode()],
                        self.seq,
                    )
                )
            elif c.startswith("capture-pane") and "-P" in c.split():
                replies.append(Reply(True, [b""], self.seq))
            elif c.startswith("capture-pane"):
                replies.append(Reply(True, [b"row one", b"row two"], self.seq))
            else:
                replies.append(Reply(True, [], self.seq))
        return replies

    async def close(self) -> None:
        self.closed = True

    def output(self, bridge: NativeBridge, pane: str, data: bytes) -> None:
        self.seq += 1
        bridge._on_output(pane, data)


def _fake_bridge() -> tuple[NativeBridge, FakeWS, FakeControl]:
    ws = FakeWS()
    bridge = NativeBridge(ws)  # type: ignore[arg-type]
    fake = FakeControl()
    bridge.client = fake  # type: ignore[assignment]
    return bridge, ws, fake


def _snapshots(bridge: NativeBridge) -> int:
    return sum(1 for f in bridge.out.frames if f.data[:1] == bytes([FRAME_SNAPSHOT]))


@pytest.mark.asyncio
async def test_overflow_with_nothing_outstanding_recovers_without_an_ack():
    bridge, _, fake = _fake_bridge()
    await bridge.snapshot("%1")
    bridge.out.frames.clear()
    bridge.out.sent = bridge.out.queued = bridge.out.acked = 0
    for _ in range(17):  # 17 x 64 KiB queued before the sender ran
        fake.output(bridge, "%1", b"y" * 65536)
    assert bridge._resync is not None  # scheduled by the drop itself
    await bridge._resync
    assert not bridge.overflow
    assert _snapshots(bridge) == 1
    assert bridge.out.unacked() <= HIGH_WATER


@pytest.mark.asyncio
async def test_output_held_during_a_capture_is_bounded_and_the_snapshot_retried():
    bridge, _, fake = _fake_bridge()
    bridge.pane = "%1"
    fake.gate = asyncio.Event()
    capture = asyncio.create_task(bridge.snapshot("%1"))
    await asyncio.sleep(0.01)
    assert bridge.snapshotting
    for _ in range(40):  # 2.5 MiB while the capture is in flight
        fake.output(bridge, "%1", b"z" * 65536)
    assert bridge.held_bytes <= native.HELD_MAX
    fake.gate.set()
    await capture
    # The spoiled capture was not sent; a fresh one was.
    assert bridge._resync is not None
    await bridge._resync
    assert _snapshots(bridge) == 1 and not bridge.overflow


@pytest.mark.asyncio
async def test_a_snapshot_waits_for_room_then_goes_out():
    bridge, _, _ = _fake_bridge()
    bridge.pane = "%1"
    bridge.out.sent = HIGH_WATER  # nothing acknowledged
    await bridge.snapshot("%1")
    assert _snapshots(bridge) == 0 and bridge.resync_wanted
    bridge.ack(HIGH_WATER)
    assert bridge._resync is not None
    await bridge._resync
    assert _snapshots(bridge) == 1


@pytest.mark.asyncio
async def test_history_waits_for_room_and_control_frames_cannot_pile_up():
    bridge, ws, _ = _fake_bridge()
    bridge.pane = "%1"
    bridge.out.sent = HIGH_WATER
    await bridge.history("%1", 100, 50)
    frame = bridge.out.frames[-1].data
    assert frame[0] == FRAME_HISTORY and b'"busy":true' in bytes(frame)
    # A page that reads nothing at all is closed rather than queued for.
    bridge.out.sent = native.HARD_LIMIT
    assert not bridge.push_control({"type": "pong"})
    await asyncio.sleep(0.05)
    assert ws.closed == (native.CLOSE_STALLED, "page stopped reading")


@pytest.mark.asyncio
async def test_session_frames_and_broadcasts_share_the_one_timed_sender(monkeypatch):
    """No second writer: the session frame and a broadcast go through the
    outbox, so a stalled socket ends the connection through the sender's
    timeout, and the client is closed."""
    monkeypatch.setattr(native, "SEND_TIMEOUT", 0.1)
    bridge, ws, fake = _fake_bridge()
    stalled = asyncio.Event()

    async def never(_data) -> None:
        stalled.set()
        await asyncio.Event().wait()

    ws.send_text = never  # type: ignore[method-assign]
    ws.send_bytes = never  # type: ignore[method-assign]
    from board.sweep import ClientSession

    bridge._report_session(ClientSession("t", "$1", 1, "@1", 0, "sh"))
    run = asyncio.create_task(bridge.run())
    await asyncio.wait_for(stalled.wait(), 2)
    await classic.broadcast_control({"type": "voice_error", "message": "x"})
    await asyncio.wait_for(run, 3)
    assert fake.closed
    assert bridge.report not in classic._client_views
    # Never confirmed as displayed: the frame was not delivered.
    assert bridge.report.last_reported is None


@pytest.mark.asyncio
async def test_start_refuses_a_running_server_that_is_too_old_or_incomplete():
    for version, history_added in (("3.4", "0"), ("3.8", "")):
        bridge, _, fake = _fake_bridge()
        original = fake.command

        async def command(*commands, timeout=15.0, _v=version, _h=history_added):
            from terminal.control import Reply

            if commands[0] == "display -p '#{version}'":
                values = dict.fromkeys(native._PANE_FIELDS, "1")
                values["history_added"] = _h
                row = "\t".join(values[f] for f in native._PANE_FIELDS).encode()
                return [Reply(True, [_v.encode()]), Reply(True, [row])]
            return await original(*commands, timeout=timeout)

        fake.command = command  # type: ignore[method-assign]

        async def started(argv, env, cwd=None):
            return None

        fake.start = started  # type: ignore[attr-defined]
        with pytest.raises(native.Unsupported):
            await bridge.start(["tmux", "-C", "attach"], "/")
        assert fake.closed


@pytest.mark.asyncio
async def test_a_stalled_tmux_probe_is_bounded_and_does_not_block_the_loop(
    tmp_path, monkeypatch
):
    fake_tmux = tmp_path / "bin" / "tmux"
    fake_tmux.parent.mkdir()
    fake_tmux.write_text("#!/bin/sh\nsleep 30\n")
    fake_tmux.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_tmux.parent}:{os.environ['PATH']}")
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    t = asyncio.create_task(ticker())
    started = time.monotonic()
    assert await native.run_tmux(["-V"], timeout=0.3) is None
    t.cancel()
    assert time.monotonic() - started < 2
    assert ticks > 10  # the loop kept running meanwhile


@pytest.mark.asyncio
async def test_endpoint_refuses_an_old_binary_when_no_server_runs(monkeypatch):
    monkeypatch.setattr(native, "verify_ws_cookie", lambda ws: True)
    monkeypatch.setattr(native.board_sweep, "run_session_sweep_checked", lambda: [])

    async def old() -> str:
        return "tmux 3.4"

    monkeypatch.setattr(native, "binary_version", old)
    ws = FakeWS()
    await native.native_ws(ws)  # type: ignore[arg-type]
    assert ws.controls("unsupported") == [
        {"type": "unsupported", "version": "tmux 3.4", "minimum": "3.8"}
    ]
    assert ws.closed is not None and ws.closed[0] == native.CLOSE_UNSUPPORTED


@pytest.mark.asyncio
async def test_endpoint_closes_1013_when_the_sweep_stalls(monkeypatch):
    monkeypatch.setattr(native, "verify_ws_cookie", lambda ws: True)
    monkeypatch.setattr(native, "PROBE_TIMEOUT", 0.1)
    monkeypatch.setattr(
        native.board_sweep, "run_session_sweep_checked", lambda: time.sleep(1)
    )
    ws = FakeWS()
    started = time.monotonic()
    await native.native_ws(ws)  # type: ignore[arg-type]
    assert ws.closed == (1013, "tmux temporarily unavailable")
    assert time.monotonic() - started < 1.0
