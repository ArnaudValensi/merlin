"""The native terminal bridge (``terminal/native.py``, ``/ws/terminal2``).

The real-tmux tests run a private server (``-S`` in ``tmp_path``, ``HOME`` in
``tmp_path`` so no personal ``~/.tmux.conf`` is read) with Merlin's own
``terminal/tmux.conf``, and drive a ``NativeBridge`` through a fake WebSocket.
Nothing reaches the user's tmux server.

The differential tests compare, with the page's own parser run by node
(``xterm_state.js``), what a page builds from the bridge's frames with what a
terminal fed the program's whole raw output shows.
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
from terminal.control import ControlClient, Reply, quote
from terminal.native import (
    FRAME_HISTORY,
    FRAME_UPDATE,
    HIGH_WATER,
    NativeBridge,
    PaneModes,
    Row,
    parse_rows,
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
XTERM_STATE = Path(__file__).with_name("xterm_state.js")


# -- a fake page --------------------------------------------------------------------


def parse_frame(data: bytes) -> tuple[int, dict, list[bytes]]:
    """A frame's kind, header and the rows the page keeps (skipped rows
    dropped: tests reading text need no attributes carried from them)."""
    head, _, body = data[1:].partition(b"\x00")
    header = json.loads(head)
    if data[0] == FRAME_HISTORY:
        count = header["skip"] + len(header["wrapped"])
        rows = body.split(b"\n", count - 1) if count else []
        return data[0], header, rows[header["skip"] :]
    hist, screen = header["hist"], header["screen"]
    n_hist = hist["skip"] + hist["count"] if hist["count"] else 0
    count = n_hist + screen["skip"] + len(screen["wrapped"])
    rows = body.split(b"\n", count - 1) if count else []
    kept = rows[hist["skip"] : n_hist] if hist["count"] else []
    return data[0], header, kept + rows[n_hist + screen["skip"] :]


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

    def frames(self, kind: int | None = None) -> list[tuple[int, dict, list[bytes]]]:
        out = [parse_frame(m) for m in self.sent if isinstance(m, bytes)]
        return [f for f in out if kind is None or f[0] == kind]

    def raw_frames(self) -> list[bytes]:
        return [m for m in self.sent if isinstance(m, bytes)]

    def history_text(self) -> list[str]:
        """The page's history, as text, from every update since the last reset."""
        lines: list[str] = []
        for _, h, rows in self.frames(FRAME_UPDATE):
            if h["reset"]:
                lines = []
            lines += [
                r.decode(errors="replace").rstrip() for r in rows[: h["hist"]["count"]]
            ]
        return lines

    def history_with_gaps(self) -> list[str | None]:
        """Like history_text, with None for each row tmux dropped before the
        page got it (a reported gap)."""
        lines: list[str | None] = []
        for _, h, rows in self.frames(FRAME_UPDATE):
            if h["reset"]:
                lines = []
            lines += [None] * h["hist"]["gap"]
            lines += [
                r.decode(errors="replace").rstrip() for r in rows[: h["hist"]["count"]]
            ]
        return lines

    def screen_text(self) -> list[str]:
        updates = self.frames(FRAME_UPDATE)
        if not updates:
            return []
        _, h, rows = updates[-1]
        return [r.decode(errors="replace").rstrip() for r in rows[h["hist"]["count"] :]]


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


def test_parse_rows_turns_relative_numbers_absolute():
    lines = [
        b"-2 W ABCDE",
        b"-1 - F \x1b[31mred\x1b[39m",
        b"0 WX \xe6\x97\xa5",
        b"1 - ",
    ]
    assert parse_rows(lines, 100) == [
        Row(98, True, b"ABCDE"),
        Row(99, False, b"F \x1b[31mred\x1b[39m"),
        Row(100, True, b"\xe6\x97\xa5"),
        Row(101, False, b""),
    ]


def _modes(**over: str) -> PaneModes:
    values = dict.fromkeys(native._PANE_FIELDS, "0")
    values.update(pane_id="%1", pane_width="10", pane_height="2", cursor_flag="1")
    values.update(over)
    return PaneModes(values)


def test_pane_state_for_the_page():
    state = _modes(
        cursor_x="10", cursor_y="1", alternate_on="1", mouse_sgr_flag="1"
    ).state()
    assert state["cursor"]["x"] == 9  # a pending wrap shows on the last column
    assert state["alternate"] and state["mouse"]["sgr"]


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


def test_attach_ladder_is_the_classic_one_in_control_mode(monkeypatch):
    from board.sweep import TmuxSession
    from terminal.tmux import SessionIdentity, tmux_conf_args

    monkeypatch.setattr(native, "server_spawn_prefix", lambda running: [])
    bridge = NativeBridge(FakeWS())  # type: ignore[arg-type]
    conf = tmux_conf_args()
    row = TmuxSession("dev", "$3", 1700000000, False, 1, 0)
    assert bridge.attach_argv([row], SessionIdentity("$3", 1700000000)) == [
        "tmux", "-C", *conf, "attach", "-t", "$3",
    ]  # fmt: skip
    assert bridge.attach_argv([row], SessionIdentity("$3", 1)) == [
        "tmux",
        "-C",
        *conf,
        "attach",
    ]
    assert bridge.attach_argv([], None) == [
        "tmux", "-C", *conf, "new-session", "-A", "-s", "merlin-dev", "-x", "120", "-y", "40",
    ]  # fmt: skip


@pytest.mark.asyncio
async def test_the_control_client_gets_the_terminal_environment(monkeypatch):
    """The client may create the server, which every shell inherits: the same
    environment rules as the classic terminal."""
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


# -- admission and delivery, in memory ------------------------------------------------


class FakeControl:
    """Stands in for the control client. ``added`` is the pane's
    history_added; its rows read "row N"; ``drift`` rows scroll in between the
    bridge's aim and each capture."""

    def __init__(self, added: int = 0, rows: int = 2, row_bytes: int = 0) -> None:
        self.added = added
        self.rows = rows
        self.collected = 0
        self.drift = 0
        self.row_bytes = row_bytes
        self.calls: list[tuple[str, ...]] = []
        self.closed = False
        self.exited = asyncio.Event()
        self.pid = None

    def _row(self, number: int) -> bytes:
        rel = number - self.added
        return f"{rel} - row {number}".encode() + b"x" * self.row_bytes

    async def command(self, *commands: str, timeout: float = 15.0):
        self.calls.append(commands)
        self.added += self.drift
        replies = []
        for c in commands:
            if "pane_width" in c:
                values = dict.fromkeys(native._PANE_FIELDS, "0")
                values.update(
                    pane_id="%1", pane_width="20", pane_height=str(self.rows),
                    cursor_flag="1", bracket_paste_flag="0",
                    history_added=str(self.added), history_collected=str(self.collected),
                    history_size=str(self.added - self.collected),
                )  # fmt: skip
                row = "\t".join(values[f] for f in native._PANE_FIELDS).encode()
                replies.append(Reply(True, [row]))
            elif c.startswith("capture-pane"):
                parts = c.split()
                start = int(parts[parts.index("-S") + 1])
                end_s = parts[parts.index("-E") + 1]
                end = self.rows - 1 if end_s == "-" else int(end_s)
                first = max(self.added + start, self.collected)
                last = self.added + end
                replies.append(
                    Reply(True, [self._row(n) for n in range(first, last + 1)])
                )
            elif c.endswith("'#{history_added}'"):
                replies.append(Reply(True, [str(self.added).encode()]))
            elif "history_added" in c:
                replies.append(
                    Reply(True, [f"{self.added}\t{self.collected}\t0".encode()])
                )
            else:
                replies.append(Reply(True, []))
        return replies

    async def close(self) -> None:
        self.closed = True


def _fake_bridge(**kw) -> tuple[NativeBridge, FakeWS, FakeControl]:
    ws = FakeWS()
    bridge = NativeBridge(ws)  # type: ignore[arg-type]
    fake = FakeControl(**kw)
    bridge.client = fake  # type: ignore[assignment]
    bridge.pane = "%1"
    return bridge, ws, fake


def _queued(bridge: NativeBridge, kind: int) -> list[tuple[int, dict, list[bytes]]]:
    return [
        parse_frame(f.data)
        for f in bridge.out.frames
        if not f.text and f.data[0] == kind
    ]


def _drain(bridge: NativeBridge) -> None:
    """The page took and acknowledged everything."""
    bridge.out.frames.clear()
    bridge.out.queued = 0
    bridge.out.acked = bridge.out.sent


def _numbers(rows: list[bytes]) -> list[int]:
    return [int(r.split()[1].rstrip(b"x")) for r in rows]


def _history_numbers(bridge: NativeBridge) -> list[int]:
    return [
        n
        for _, h, rows in _queued(bridge, FRAME_UPDATE)
        for n in _numbers(rows[: h["hist"]["count"]])
    ]


@pytest.mark.asyncio
async def test_updates_send_every_history_row_once_even_when_output_scrolls_meanwhile():
    bridge, _, fake = _fake_bridge(added=50)
    await bridge.update()  # the reset: the history so far and the screen
    ((_, first, rows),) = _queued(bridge, FRAME_UPDATE)
    assert first["reset"] and _numbers(rows[: first["hist"]["count"]]) == list(
        range(50)
    )
    assert _numbers(rows[first["hist"]["count"] :]) == [50, 51]
    _drain(bridge)
    # 30 rows scroll in, and 7 more between the aim and the capture.
    fake.added += 30
    fake.drift = 7
    await bridge.update()
    fake.drift = 0
    await bridge.update()
    assert bridge.sent_next >= 80
    assert _history_numbers(bridge) == list(range(50, bridge.sent_next))


@pytest.mark.asyncio
async def test_rows_tmux_dropped_are_reported_as_a_gap():
    bridge, _, fake = _fake_bridge(added=10)
    await bridge.update()
    _drain(bridge)
    fake.added, fake.collected = 5000, 3000
    await bridge.update()  # learns where the pane is: the gap
    await bridge.update()  # then the rows tmux still has
    frames = _queued(bridge, FRAME_UPDATE)
    assert sum(h["hist"]["gap"] for _, h, _ in frames) == 3000 - 10
    assert _history_numbers(bridge)[0] == 3000


@pytest.mark.asyncio
async def test_a_history_batch_is_capped_and_the_rest_follows():
    bridge, _, fake = _fake_bridge(added=0, row_bytes=1000)
    await bridge.update()
    _drain(bridge)
    fake.added = 1500  # 1500 rows of about 1 KB
    sent: list[int] = []
    for _ in range(30):
        bridge.dirty.clear()
        await bridge.update()
        for _, h, rows in _queued(bridge, FRAME_UPDATE):
            assert (
                sum(len(r) for r in rows[: h["hist"]["count"]])
                <= native.HISTORY_BYTES_MAX
            )
            sent += _numbers(rows[: h["hist"]["count"]])
        _drain(bridge)
        if not bridge.dirty.is_set():
            break
    assert sent == list(range(1500))


@pytest.mark.asyncio
async def test_no_update_while_the_page_is_behind_then_one_when_it_catches_up():
    bridge, _, _ = _fake_bridge(added=5)
    bridge.out.sent = HIGH_WATER  # nothing acknowledged
    await bridge.update()
    assert _queued(bridge, FRAME_UPDATE) == [] and not bridge.room.is_set()
    assert bridge.dirty.is_set()  # still due
    bridge.ack(HIGH_WATER)
    assert bridge.room.is_set()
    await bridge.update()
    assert len(_queued(bridge, FRAME_UPDATE)) == 1


@pytest.mark.asyncio
async def test_a_frame_bigger_than_the_window_goes_out_only_alone():
    bridge, _, _ = _fake_bridge(added=0, rows=200, row_bytes=8000)  # a 1.6 MB screen
    bridge.out.sent = 10  # something outstanding
    await bridge.update()
    assert _queued(bridge, FRAME_UPDATE) == []
    bridge.ack(10)
    await bridge.update()
    assert len(_queued(bridge, FRAME_UPDATE)) == 1
    assert len(bridge.out.frames[0].data) > HIGH_WATER


@pytest.mark.asyncio
async def test_history_waits_for_room_and_nothing_piles_up_past_the_hard_limit():
    bridge, ws, _ = _fake_bridge(added=100)
    bridge.out.sent = HIGH_WATER
    await bridge.history("%1", 100, 50)
    ((_, h, _),) = _queued(bridge, FRAME_HISTORY)
    assert h["busy"]
    # A page that reads nothing at all is closed, busy replies included.
    bridge.out.sent = native.HARD_LIMIT
    queued = bridge.out.queued
    for _ in range(100):
        await bridge.history("%1", 100, 50)
    assert bridge.out.queued == queued
    assert not bridge.push_control({"type": "pong"})
    await asyncio.sleep(0.05)
    assert ws.closed == (native.CLOSE_STALLED, "page stopped reading")


@pytest.mark.asyncio
async def test_history_ranges_by_absolute_number_with_drift():
    bridge, _, fake = _fake_bridge(added=500)
    bridge.added_seen = 500
    fake.drift = 3
    await bridge.history("%1", 400, 100)
    ((_, h, rows),) = _queued(bridge, FRAME_HISTORY)
    assert (h["start"], h["end"]) == (300, 400)
    assert _numbers(rows) == list(range(300, 400))


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
    # Never delivered: never counted as displayed.
    assert bridge.report.last_reported is None


@pytest.mark.asyncio
async def test_start_refuses_a_running_server_that_is_too_old_or_incomplete():
    for version, history_added in (("3.4", "0"), ("3.8", "")):
        bridge, _, fake = _fake_bridge()

        async def command(*commands, timeout=15.0, _v=version, _h=history_added):
            values = dict.fromkeys(native._PANE_FIELDS, "1")
            values["history_added"] = _h
            row = "\t".join(values[f] for f in native._PANE_FIELDS).encode()
            return [Reply(True, [_v.encode()]), Reply(True, [row])]

        async def started(argv, env, cwd=None):
            return None

        fake.command = command  # type: ignore[method-assign]
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
    assert ticks > 10


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


@pytest.mark.asyncio
async def test_endpoint_rejects_without_a_cookie(monkeypatch):
    monkeypatch.setattr(native, "verify_ws_cookie", lambda ws: False)
    ws = FakeWS()
    await native.native_ws(ws)  # type: ignore[arg-type]
    assert ws.closed == (4401, "Unauthorized")


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


def _keep_acking(bridge: NativeBridge) -> None:
    async def ack() -> None:
        while True:
            bridge.ack(bridge.out.sent)
            await asyncio.sleep(0.01)

    bridge._spawn(ack())


async def _bridge(
    socket: str, session: str = "t", *, acking: bool = True
) -> tuple[NativeBridge, FakeWS, asyncio.Task]:
    """A bridge on ``session`` and a page that acknowledges what it gets."""
    ws = FakeWS()
    bridge = NativeBridge(ws, tmux=("tmux", "-S", socket))  # type: ignore[arg-type]
    await bridge.start(
        ["tmux", "-S", socket, "-C", "attach", "-t", session], str(Path.cwd())
    )
    task = asyncio.create_task(bridge.run())
    if acking:
        _keep_acking(bridge)
    assert await _until(lambda: bool(ws.frames(FRAME_UPDATE)))
    return bridge, ws, task


async def _stop(bridge: NativeBridge, task: asyncio.Task) -> None:
    await bridge.close()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _run(bridge: NativeBridge, command: str) -> None:
    await bridge.send_input(bridge.pane, command + "\r")


@needs_tmux
@pytest.mark.asyncio
async def test_attach_reports_session_windows_and_the_pane(server):
    socket, tmux, _ = server
    bridge, ws, task = await _bridge(socket)
    try:
        session = ws.controls("session")[-1]
        assert session["name"] == "t" and session["window_id"].startswith("@")
        assert [w["active"] for w in ws.controls("windows")[-1]["windows"]] == [True]
        _, header, _ = ws.frames(FRAME_UPDATE)[0]
        assert header["reset"] and header["pane"] == bridge.pane
        assert (header["cols"], header["rows"]) == (80, 24)
        assert classic.looking_at(f"t:{session['window_id']}")
        assert tmux(
            "list-clients", "-F", "#{client_control_mode}|#{client_tty}"
        ).split() == ["1|"]
        await _run(bridge, "echo hello-$((40+2))")
        assert await _until(lambda: "hello-42" in ws.screen_text())
    finally:
        await _stop(bridge, task)
    assert not classic.looking_at(f"t:{session['window_id']}")
    assert tmux("ls", "-F", "#{session_name}").split() == ["t"]
    assert tmux("list-clients").strip() == ""


@needs_tmux
@pytest.mark.asyncio
async def test_a_flood_reaches_the_page_row_by_row_once_each(server):
    socket, _, _ = server
    bridge, ws, task = await _bridge(socket)
    try:
        n = 40_000  # under tmux.conf's history-limit (50000)
        await _run(bridge, f"seq 1 {n}; echo DONE-$((40+2))")
        assert await _until(lambda: "DONE-42" in ws.screen_text(), timeout=30)
        # The screen is current at once; the history follows in batches.
        assert await _until(
            lambda: bridge.sent_next >= bridge.added_seen and not bridge.out.frames,
            timeout=30,
        )
        numbers = [t for t in ws.history_text() + ws.screen_text() if t.isdigit()]
        assert numbers == [str(i) for i in range(1, n + 1)]
    finally:
        await _stop(bridge, task)


@needs_tmux
@pytest.mark.asyncio
async def test_a_page_that_stops_acknowledging_gets_no_more_than_the_window(server):
    socket, _, _ = server
    bridge, ws, task = await _bridge(socket, acking=False)
    try:
        bridge.ack(bridge.out.sent)
        await _run(bridge, "seq 1 400000; echo DONE-$((40+3))")
        await asyncio.sleep(2.0)
        assert bridge.out.unacked() <= HIGH_WATER + native.HISTORY_BYTES_MAX
        _keep_acking(bridge)
        assert await _until(lambda: "DONE-43" in ws.screen_text(), timeout=60)
        assert await _until(
            lambda: bridge.sent_next >= bridge.added_seen and not bridge.out.frames,
            timeout=60,
        )
        # 400000 rows overflow tmux's history-limit (50000) long before the
        # page reads them: what tmux dropped is reported as gaps, the rest
        # arrives once each, in order.
        rows = ws.history_with_gaps() + ws.screen_text()
        first = next(i for i, r in enumerate(rows) if r is not None and r.isdigit())
        tail = [r for r in rows[first:] if r is None or r.isdigit()]
        assert tail[0] is not None
        start = int(tail[0])
        expected = [str(i) for i in range(start, 400_001)]
        got = [r if r is not None else e for r, e in zip(tail, expected, strict=False)]
        bad = next(
            (i for i, (g, e) in enumerate(zip(got, expected, strict=False)) if g != e),
            None,
        )
        assert (len(tail), bad) == (len(expected), None), (
            tail[bad - 3 : bad + 3] if bad is not None else tail[-3:]
        )
        assert sum(1 for r in tail if r is None) > 0
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
        tmux("set", "-w", "-t", window, "@agent_state", "busy")
        out = tmp / "esc"
        await _run(bridge, f"stty raw -echo; head -c 6 > {out}; stty sane")
        await asyncio.sleep(0.3)
        # Escape on a busy agent window: tmux.conf's binding marks it idle and
        # still sends the key to the pane.
        await bridge.send_key(bridge.pane, "Escape")
        assert await _until(
            lambda: tmux("show", "-wv", "-t", window, "@agent_state").strip() == "idle"
        )
        await bridge.send_key(bridge.pane, "Up")
        await bridge.send_key(bridge.pane, "M-.")
        assert await _until(lambda: out.exists() and out.stat().st_size >= 6)
        assert out.read_bytes() == b"\x1b\x1b[A\x1b."
        out2 = tmp / "app"
        await _run(
            bridge, f"printf '\\033[?1h'; stty raw -echo; head -c 3 > {out2}; stty sane"
        )
        await asyncio.sleep(0.3)
        await bridge.send_key(bridge.pane, "Up")
        assert await _until(lambda: out2.exists() and out2.stat().st_size >= 3)
        assert out2.read_bytes() == b"\x1bOA"
        await bridge.send_key(bridge.pane, "F2")
        assert await _until(lambda: len(ws.controls("windows")[-1]["windows"]) == 2)
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
                h["cols"] == 100 and h["reset"] for _, h, _ in ws.frames(FRAME_UPDATE)
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
async def test_history_rows_by_absolute_number(server):
    socket, tmux, _ = server
    bridge, ws, task = await _bridge(socket)
    try:
        await _run(bridge, "clear; seq 1 3000")
        assert await _until(lambda: "3000" in ws.screen_text())
        added = int(
            tmux("display", "-p", "-t", bridge.pane, "#{history_added}").strip()
        )
        ws.push(
            {"type": "history", "pane": bridge.pane, "end": added - 100, "count": 50}
        )
        assert await _until(lambda: bool(ws.frames(FRAME_HISTORY)))
        _, header, rows = ws.frames(FRAME_HISTORY)[0]
        assert header["end"] - header["start"] == 50 and len(rows) == 50
        values = [int(r.decode().strip()) for r in rows]
        assert values == list(range(values[0], values[0] + 50))
    finally:
        await _stop(bridge, task)


# -- the clipboard -------------------------------------------------------------------


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
    """A classic client and a native page on one session: whichever client
    tmux calls current, the classic tty gets its OSC 52 and the native page a
    clipboard frame, through the shell path and a provider path (stdout not
    the pane, as when NeoVim runs merlin-clip)."""
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
    finally:
        classic_client.close()
        await _stop(bridge, task)


@needs_tmux
@pytest.mark.asyncio
async def test_clipboard_from_merlin_clip_and_from_a_program_osc52(server):
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
        # A program writing OSC 52 itself (NeoVim's own provider): tmux keeps
        # it in a paste buffer (set-clipboard on), and the page gets it.
        await _run(bridge, "printf '\\033]52;c;%s\\007' $(printf from-osc | base64)")
        assert await _until(
            lambda: any(c.get("text") == "from-osc" for c in ws.controls("clipboard"))
        )
    finally:
        await _stop(bridge, task)


# -- differential: the page's view against the program's whole output -----------------


def _xterm(cols: int, rows: int, inputs: dict, history: int = 5) -> dict:
    """``history``: how many of the last history rows to compare (0: all)."""
    request = {
        "cols": cols,
        "rows": rows,
        "history": history,
        "inputs": {
            name: {kind: [base64.b64encode(c).decode() for c in chunks]}
            for name, (kind, chunks) in inputs.items()
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


def _diff(got: dict, want: dict) -> list[str]:
    out = []
    for key in ("alternate", "cursor"):
        if got[key] != want[key]:
            out.append(f"{key}: {got[key]!r} != {want[key]!r}")
    for part in ("screen", "history"):
        if len(got[part]) != len(want[part]):
            out.append(f"{part}: {len(got[part])} rows != {len(want[part])}")
            continue
        for y, (g, w) in enumerate(zip(got[part], want[part], strict=True)):
            if g != w:

                def text(row: dict) -> str:
                    return "".join(c[0] or " " for c in row["cells"])

                cells = [
                    x
                    for x, (a, b) in enumerate(zip(g["cells"], w["cells"], strict=True))
                    if a != b
                ]
                out.append(
                    f"{part}[{y}]: {text(g)!r} != {text(w)!r} "
                    f"wrapped {g['wrapped']}/{w['wrapped']} cells {cells[:5]}"
                )
    return out


# Each case: what the program writes before the page attaches, then after.
# The program waits on files between the steps, so the page attaches after the
# first part is written (asserted) and before the second.
DIFFERENTIAL = {
    "rendition": (r"\033[31;1;4mA", r"B\033[0mC"),
    "rendition-256-and-rgb": (r"\033[38;5;208;48;2;1;2;3mA", r"B"),
    "pending-wrap": (r"%020d", r"F"),
    "cut-escape": (r"x\033[3", r"1mR"),
    "cut-utf8": (r"a\303", r"\251b"),
    "alternate-enter": (r"main\r\n\033[?1049h\033[Halt", r"\033[?1049lback"),
    "alternate-leave": (r"one\r\n\033[?1049h\033[Hfull", r"!\033[?1049ltwo"),
    "charset": (r"\033(0lqk", r"x\033(Bq"),
    "shift-out": (r"\033)0\016lq", r"k\017q"),
    "saved-cursor": (r"\033[2;3H\0337\033[1;1HA", r"\0338Z"),
    "origin": (r"\033[2;4r\033[?6h\033[2;3Hin", r"\033[1;1Hz"),
    "no-wrap": (r"\033[?7l%025d", r"Q"),
    "scrolled": (r"1\r\n2\r\n3\r\n4\r\n5\r\n6\r\n7\r\n8\r\n\033[33m", r"9\r\n10"),
    "wrapped-long": (r"%045d\r\nshort\r\n%030d", r"%025d"),
    # Attributes and the shift state carry from row to row in a capture.
    "full-width-colour": (r"\033[31m%060d", r"X"),
    "graphics-rows": (r"\033)0\016" + "q" * 60, r"q"),
    "colour-across-history": (r"\033[31m%0200d", r"X"),
}


def _program(
    tmux, tmp: Path, session: str, before: str, after: str, size
) -> tuple[Path, Path, Path, Path]:
    """A program in its own session that waits for ``start``, writes
    ``before``, says ``ready``, waits for ``go``, writes ``after``, says
    ``done``."""
    start, ready, go, done = (
        tmp / f"{session}.{x}" for x in ("start", "ready", "go", "done")
    )
    script = tmp / f"{session}.sh"
    script.write_text(
        f"while [ ! -f {start} ]; do sleep 0.02; done; printf '{before}'; touch {ready}; "
        f"while [ ! -f {go} ]; do sleep 0.02; done; printf '{after}'; touch {done}; sleep 60\n"
    )
    cols, rows = size
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
    return start, ready, go, done


async def _recorder(
    socket: str, session: str
) -> tuple[ControlClient, dict[str, bytearray]]:
    """A control client that sees every byte of the session's panes."""
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    recorded: dict[str, bytearray] = {}
    recorder = ControlClient(
        lambda pane, data: recorded.setdefault(pane, bytearray()).extend(data),
        lambda name, rest: None,
    )
    await recorder.start(["tmux", "-S", socket, "-C", "attach", "-t", session], env)
    return recorder, recorded


@needs_tmux
@needs_node
@pytest.mark.asyncio
async def test_the_page_matches_the_whole_stream_whatever_came_before_it_attached(
    server,
):
    """The page attaches after the program wrote the first part (it never saw
    those bytes) and gets the second part live; what it shows must equal a
    terminal fed the whole output from the first byte: cells, attributes, wrap
    flags, the cursor, both screens, the recent history."""
    socket, tmux, tmp = server
    cols, rows = 20, 6

    async def one(index: int, case: str) -> tuple[str, list[str]]:
        before, after = DIFFERENTIAL[case]
        session = f"d{index}"
        start, ready, go, done = _program(
            tmux, tmp, session, before, after, (cols, rows)
        )
        recorder, recorded = await _recorder(socket, session)
        start.touch()
        try:
            if not await _until(ready.exists):
                return case, ["the program never wrote its first part"]
            await asyncio.sleep(0.1)
            bridge, ws, task = await _bridge(socket, session)
            try:
                pane = bridge.pane
                go.touch()
                if not await _until(done.exists):
                    return case, ["the program never wrote its second part"]
                await asyncio.sleep(0.3)
                states = _xterm(
                    cols,
                    rows,
                    {
                        "reference": ("stream", [bytes(recorded.get(pane, b""))]),
                        "page": ("frames", ws.raw_frames()),
                    },
                )
                page_text = "".join(
                    c[0] for row in states["page"]["screen"] for c in row["cells"]
                ).strip()
                if not page_text:
                    return case, [
                        "the page shows nothing: the comparison would be empty"
                    ]
                return case, _diff(states["page"], states["reference"])
            finally:
                await _stop(bridge, task)
        finally:
            await recorder.close()

    results = await asyncio.gather(
        *(one(i, c) for i, c in enumerate(sorted(DIFFERENTIAL)))
    )
    assert {case: diff for case, diff in results if diff} == {}


@needs_tmux
@needs_node
@pytest.mark.asyncio
async def test_reconnect_and_an_unobserved_session_show_the_current_pane(server):
    """A page attaching again (a reconnect) after the program set state, then
    coming back to a session that changed while it showed another one, shows
    exactly what the whole stream gives."""
    socket, tmux, tmp = server
    cols, rows = 20, 6
    start, ready, go, done = _program(
        tmux, tmp, "r", r"\033[35mpre\r\n\0337", r"mid\0338post", (cols, rows)
    )
    recorder, recorded = await _recorder(socket, "r")
    start.touch()
    try:
        assert await _until(ready.exists)
        first, _, task1 = await _bridge(socket, "r")
        pane = first.pane
        await _stop(first, task1)
        second, ws, task2 = await _bridge(socket, "r")
        try:
            ws.push({"type": "switch", "target": "t"})
            assert await _until(lambda: ws.controls("session")[-1]["name"] == "t")
            go.touch()
            assert await _until(done.exists)
            ws.push({"type": "switch", "target": "r"})
            assert await _until(lambda: ws.controls("session")[-1]["name"] == "r")
            await asyncio.sleep(0.3)
            frames = ws.raw_frames()
            resets = [
                i
                for i, f in enumerate(frames)
                if parse_frame(f)[1].get("reset") and parse_frame(f)[1]["pane"] == pane
            ]
            states = _xterm(
                cols,
                rows,
                {
                    "reference": ("stream", [bytes(recorded[pane])]),
                    "page": ("frames", frames[resets[-1] :]),
                },
            )
            assert _diff(states["page"], states["reference"]) == []
        finally:
            await _stop(second, task2)
    finally:
        await recorder.close()


@needs_tmux
@needs_node
@pytest.mark.asyncio
async def test_history_batches_and_ranges_keep_attributes(server, monkeypatch):
    """Attributes set once and carried over hundreds of rows survive every way
    history reaches the page: the reset's recent rows, live batches cut small,
    and older ranges asked for one by one down to the first row."""
    monkeypatch.setattr(native, "INITIAL_HISTORY", 20)
    monkeypatch.setattr(native, "HISTORY_BYTES_MAX", 300)
    socket, tmux, tmp = server
    cols, rows = 20, 6
    start, ready, go, done = (tmp / f"h.{x}" for x in ("start", "ready", "go", "done"))
    script = tmp / "h.sh"
    script.write_text(
        f"while [ ! -f {start} ]; do sleep 0.02; done; printf '\\033[32;1m'; seq 1 200; "
        f"touch {ready}; while [ ! -f {go} ]; do sleep 0.02; done; "
        f"printf '\\033[0;35m'; seq 201 260; printf '\\033[4mend'; touch {done}; sleep 60\n"
    )
    tmux(
        "new-session", "-d", "-s", "h", "-x", str(cols), "-y", str(rows), f"sh {script}"
    )
    recorder, recorded = await _recorder(socket, "h")
    start.touch()
    try:
        assert await _until(ready.exists)
        await asyncio.sleep(0.1)
        bridge, ws, task = await _bridge(socket, "h")
        try:
            go.touch()
            assert await _until(done.exists)
            assert await _until(
                lambda: bridge.sent_next >= bridge.added_seen and not bridge.out.frames
            )
            await asyncio.sleep(0.2)
            # The page asks for older rows until it has them all.
            oldest = next(
                h["hist"]["start"] for _, h, _ in ws.frames(FRAME_UPDATE) if h["reset"]
            )
            while oldest > 0:
                seen = len(ws.frames(FRAME_HISTORY))
                ws.push(
                    {"type": "history", "pane": bridge.pane, "end": oldest, "count": 50}
                )
                assert await _until(lambda: len(ws.frames(FRAME_HISTORY)) > seen)
                oldest = ws.frames(FRAME_HISTORY)[-1][1]["start"]
            states = _xterm(
                cols,
                rows,
                {
                    "reference": ("stream", [bytes(recorded[bridge.pane])]),
                    "page": ("frames", ws.raw_frames()),
                },
                history=0,
            )
            assert len(states["page"]["history"]) > 200
            assert _diff(states["page"], states["reference"]) == []
        finally:
            await _stop(bridge, task)
    finally:
        await recorder.close()


@needs_node
@pytest.mark.asyncio
async def test_a_capture_whose_skipped_prefix_is_long_still_delivers_its_rows():
    """After a burst the aim's margin makes the skipped prefix long (bounded by
    one capture); the page's decoder keeps every row the frame advertises."""
    bridge, ws, fake = _fake_bridge(added=40000)
    await bridge.update()
    _drain(bridge)
    fake.drift = 6000
    await bridge.update()
    _drain(bridge)
    fake.drift = 0
    await bridge.update()
    frames = [f.data for f in bridge.out.frames if not f.text]
    ((_, header, _),) = [parse_frame(f) for f in frames if f[0] == FRAME_UPDATE]
    assert header["hist"]["count"] > 0
    assert header["hist"]["skip"] <= native.ROWS_PER_CAPTURE + 1
    state = _xterm(20, 2, {"page": ("frames", frames)}, history=0)
    page_rows = [
        "".join(c[0] for c in row["cells"]).strip() for row in state["page"]["history"]
    ]
    start = header["hist"]["start"]
    assert page_rows == [
        f"row {n}" for n in range(start, start + header["hist"]["count"])
    ]


@pytest.mark.asyncio
async def test_a_range_of_large_rows_shrinks_until_it_fits_whatever_it_takes():
    bridge, _, _ = _fake_bridge(added=3000, row_bytes=20000)
    bridge.added_seen = 3000
    await bridge.history("%1", 2500, 2000)
    ((_, h, rows),) = _queued(bridge, FRAME_HISTORY)
    assert h["end"] == 2500 and rows and _numbers(rows)[-1] == 2499
    assert sum(len(r) for r in rows) <= native.HISTORY_BYTES_MAX + 20100


@pytest.mark.asyncio
async def test_a_range_that_never_catches_up_with_scrolling_gets_busy():
    bridge, _, fake = _fake_bridge(added=3000)
    fake.drift = 100_000  # every aim misses
    await bridge.history("%1", 2500, 100)
    ((_, h, rows),) = _queued(bridge, FRAME_HISTORY)
    assert h["busy"] and rows == []
