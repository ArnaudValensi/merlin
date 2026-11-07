"""The tmux control-mode protocol layer (``terminal/control.py``).

The real-tmux tests run on a private server (``-S`` in ``tmp_path``), never the
user's: every command names its socket. They pin the facts the native
terminal's bridge is built on: exact bytes both ways, one reply block per
command, and the in-stream order that makes a capture plus the live stream
lose and double nothing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time

import pytest

from terminal.control import (
    ControlClient,
    Reply,
    control_env,
    hex_keys,
    quote,
    unescape,
)

HAS_TMUX = shutil.which("tmux") is not None
needs_tmux = pytest.mark.skipif(not HAS_TMUX, reason="tmux not installed")


# -- pure protocol ------------------------------------------------------------


def test_unescape_restores_every_byte():
    raw = bytes(range(256))
    escaped = b"".join(
        b"\\%03o" % b if b < 0x20 or b == 0x5C else bytes([b]) for b in raw
    )
    assert unescape(escaped) == raw


def test_unescape_keeps_digits_after_an_escape():
    # tmux always writes three octal digits, so a digit after one is data.
    assert unescape(b"\\0151") == b"\r1"


def test_quote_survives_single_quotes_and_separators():
    assert quote("a'b;c") == "'a'\\''b;c'"


def test_hex_keys():
    assert hex_keys(b"\x1bA") == "1b 41"


def _client() -> tuple[ControlClient, list, list]:
    outputs: list = []
    notes: list = []
    client = ControlClient(
        lambda p, d: outputs.append((p, d)), lambda n, r: notes.append((n, r))
    )
    return client, outputs, notes


@pytest.mark.asyncio
async def test_feed_routes_blocks_output_and_notifications():
    client, outputs, notes = _client()
    loop = asyncio.get_running_loop()
    client._started = loop.create_future()
    # The attach reply comes first and is nobody's.
    client._feed(b"%begin 1 1 0")
    client._feed(b"%end 1 1 0")
    assert client._started.done()
    first, second = loop.create_future(), loop.create_future()
    client._pending.extend([[first], [second]])
    client._feed(b"%output %3 a\\015\\012b")
    client._feed(b"%begin 2 5 1")
    client._feed(b"line one")
    client._feed(b"%end 2 5 1")
    # A block tmux ran for a key binding (flags 0) answers no command.
    client._feed(b"%begin 2 6 0")
    client._feed(b"%end 2 6 0")
    client._feed(b"%window-add @4")
    client._feed(b"%begin 2 7 1")
    client._feed(b"no such pane")
    client._feed(b"%error 2 7 1")
    client._feed(b"%extended-output %3 12 : z\\134")
    assert first.result() == Reply(True, [b"line one"], seq=1)
    assert second.result() == Reply(False, [b"no such pane"], seq=1)
    assert outputs == [("%3", b"a\r\nb"), ("%3", b"z\\")]
    assert notes == [("window-add", b"@4")]


@pytest.mark.asyncio
async def test_only_the_matching_terminator_ends_a_block():
    """Captured pane text that looks like the protocol stays content: only the
    %end / %error repeating the %begin's arguments closes the reply."""
    client, outputs, notes = _client()
    loop = asyncio.get_running_loop()
    client._started = loop.create_future()
    client._started.set_result(None)
    capture, after = loop.create_future(), loop.create_future()
    client._pending.extend([[capture], [after]])
    content = [
        b"before",
        b"%end this is terminal content",
        b"%error 100 7 0",
        b"%end 100 7",
        b"%begin 5 6 1",
        b"%output %1 x",
        b"%window-add @9",
        b"after",
    ]
    client._feed(b"%begin 100 7 1")
    for line in content:
        client._feed(line)
    client._feed(b"%end 100 7 1")
    client._feed(b"%begin 100 8 1")
    client._feed(b"next")
    client._feed(b"%end 100 8 1")
    assert capture.result() == Reply(True, content)
    assert after.result().lines == [b"next"]
    assert outputs == [] and notes == []


@pytest.mark.asyncio
async def test_reply_carries_the_stream_position_of_its_begin():
    client, outputs, _ = _client()
    loop = asyncio.get_running_loop()
    client._started = loop.create_future()
    client._started.set_result(None)
    fut = loop.create_future()
    client._pending.append([fut])
    client._feed(b"%output %1 a")
    client._feed(b"%output %1 b")
    client._feed(b"%begin 1 2 1")
    client._feed(b"%end 1 2 1")
    client._feed(b"%output %1 c")
    assert fut.result().seq == 2
    assert client.seq == 3


class _StuckStdin:
    """A stdin whose pipe is full: writes are accepted, drain never returns."""

    def __init__(self) -> None:
        self.lines: list[bytes] = []
        self.aborted = False
        stdin = self

        class _Transport:
            def abort(self) -> None:
                stdin.aborted = True

        self.transport = _Transport()

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        await asyncio.Event().wait()

    def close(self) -> None:
        pass


class _ExitedProc:
    pid = 0  # never signalled: wait() answers at once

    def __init__(self) -> None:
        self.stdin = _StuckStdin()

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_a_stalled_stdin_times_out_and_abandons_the_client():
    from terminal.control import ControlError

    client, _, _ = _client()
    client._proc = _ExitedProc()  # type: ignore[assignment]
    started = time.monotonic()
    with pytest.raises(ControlError):
        await client.command("display -p one", timeout=0.05)
    assert time.monotonic() - started < 1.0
    # A second caller is refused at once instead of queueing behind the stall.
    with pytest.raises(ControlError):
        await client.command("display -p two", timeout=5)
    assert client._closing is not None
    await client._closing
    assert client._proc.stdin.aborted
    assert not client._pending


# -- real tmux ------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    socket = str(tmp_path / "s")
    env = control_env({**os.environ, "SHELL": "/bin/sh"})

    def tmux(*args: str) -> str:
        return subprocess.run(
            ["tmux", "-S", socket, *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        ).stdout

    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g history-limit 200000\nset -g status off\n")
    tmux(
        "-f",
        str(conf),
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
        yield socket, env, tmux, tmp_path
    finally:
        subprocess.run(
            ["tmux", "-S", socket, "kill-server"],
            capture_output=True,
            env=env,
            check=False,
        )


class Recorder:
    def __init__(self) -> None:
        self.out: dict[str, bytearray] = {}
        self.notes: list[tuple[str, bytes]] = []

    def output(self, pane: str, data: bytes) -> None:
        self.out.setdefault(pane, bytearray()).extend(data)

    def note(self, name: str, rest: bytes) -> None:
        self.notes.append((name, rest))


async def _attach(socket, env) -> tuple[ControlClient, Recorder]:
    rec = Recorder()
    client = ControlClient(rec.output, rec.note)
    await client.start(["tmux", "-S", socket, "-C", "attach", "-t", "t"], env)
    return client, rec


async def _until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def _pane(client: ControlClient) -> str:
    return (await client.run("display -p '#{pane_id}'")).text()[0]


async def _run_in_pane(client, pane, command: str) -> None:
    await client.run(f"send-keys -t {pane} -l {quote(command)}")
    await client.run(f"send-keys -t {pane} Enter")


@needs_tmux
@pytest.mark.asyncio
async def test_control_client_has_no_tty_and_detaches_without_killing(server):
    socket, env, tmux, _ = server
    client, _rec = await _attach(socket, env)
    rows = (
        await client.run("list-clients -F '#{client_tty}|#{client_control_mode}'")
    ).text()
    assert rows == ["|1"]
    pid = client.pid
    await client.close()
    assert tmux("ls", "-F", "#{session_name}").split() == ["t"]
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


@needs_tmux
@pytest.mark.asyncio
async def test_one_reply_per_command_in_a_line(server):
    socket, env, _, _ = server
    client, _ = await _attach(socket, env)
    try:
        replies = await client.command("display -p one", "display -p two")
        assert [r.text() for r in replies] == [["one"], ["two"]]
        # A failed command skips the rest of its line, with no block for them:
        # the next line's replies still go to the next line.
        failed = await client.command(
            "display -p a", "select-window -t @999", "display -p c"
        )
        assert [r.ok for r in failed] == [True, False, False]
        assert (await client.run("display -p after")).text() == ["after"]
    finally:
        await client.close()


@needs_tmux
@pytest.mark.asyncio
async def test_every_output_byte_arrives_exactly(server):
    socket, env, _, tmp = server
    client, rec = await _attach(socket, env)
    try:
        pane = await _pane(client)
        payload = bytes(range(1, 256))
        (tmp / "bytes").write_bytes(payload)
        await _run_in_pane(client, pane, f"stty raw -echo; cat {tmp}/bytes; stty sane")
        assert await _until(lambda: payload in rec.out.get(pane, b""))
    finally:
        await client.close()


@needs_tmux
@pytest.mark.asyncio
async def test_input_bytes_reach_the_pane_exactly_once(server):
    socket, env, _, tmp = server
    client, _ = await _attach(socket, env)
    try:
        pane = await _pane(client)
        payload = (
            "Enter C-c Space ; \\ \\; 'q' \"d\" #{pane_id} $HOME é 日本 🎉 \x1b[A\t".encode()
            + bytes([0x01, 0x03, 0x04, 0x7F, 0x1B, 0x0D])
        )
        out = tmp / "typed"
        await _run_in_pane(
            client, pane, f"stty raw -echo; head -c {len(payload)} > {out}; stty sane"
        )
        await asyncio.sleep(0.3)
        await client.run(f"send-keys -t {pane} -H {hex_keys(payload)}")
        assert await _until(lambda: out.exists() and out.stat().st_size >= len(payload))
        await asyncio.sleep(0.1)
        assert out.read_bytes() == payload
    finally:
        await client.close()


@needs_tmux
@pytest.mark.asyncio
async def test_send_keys_hex_line_size_limit(server):
    """A 1 KiB chunk is one valid command; 16 KiB overflows tmux's command
    parser. Larger input goes through a paste buffer."""
    socket, env, _, _ = server
    client, _ = await _attach(socket, env)
    try:
        pane = await _pane(client)
        (small,) = await client.command(
            f"send-keys -t {pane} -H {hex_keys(b'a' * 1024)}"
        )
        (big,) = await client.command(
            f"send-keys -t {pane} -H {hex_keys(b'a' * 16384)}"
        )
        assert small.ok
        assert not big.ok
    finally:
        await client.close()


@needs_tmux
@pytest.mark.asyncio
async def test_paste_buffer_delivers_a_megabyte_exactly(server):
    socket, env, _, tmp = server
    client, _ = await _attach(socket, env)
    try:
        pane = await _pane(client)
        payload = bytes((i * 7) % 95 + 32 for i in range(1_000_000))
        out = tmp / "pasted"
        await _run_in_pane(
            client, pane, f"stty raw -echo; head -c {len(payload)} > {out}; stty sane"
        )
        await asyncio.sleep(0.3)
        src = tmp / "buf"
        src.write_bytes(payload)
        replies = await client.command(
            f"load-buffer -b nt {quote(str(src))}",
            f"paste-buffer -d -r -b nt -t {pane}",
        )
        assert all(r.ok for r in replies)
        assert await _until(lambda: out.exists() and out.stat().st_size >= len(payload))
        assert out.read_bytes() == payload
    finally:
        await client.close()


@needs_tmux
@pytest.mark.asyncio
async def test_capture_then_live_stream_loses_and_doubles_nothing(server):
    """Mid-flood, the bytes before a capture's reply are in the capture and the
    bytes after it are not: capture + live output is the exact sequence."""
    socket, env, _, _ = server
    client, rec = await _attach(socket, env)
    try:
        pane = await _pane(client)
        n = 100_000
        await _run_in_pane(client, pane, f"clear; seq 1 {n}; echo DONE-$((40+2))")
        await _until(lambda: len(rec.out.get(pane, b"")) > 20_000)
        marks: list[int] = []
        original = client._feed

        def feed(line: bytes) -> None:
            # Where the live stream stood when the capture's reply began.
            if line.startswith(b"%begin ") and not marks:
                marks.append(len(rec.out.get(pane, b"")))
            original(line)

        client._feed = feed
        history, screen, cursor = await client.command(
            f"capture-pane -p -J -S - -E -1 -t {pane}",
            f"capture-pane -p -J -t {pane}",
            f"display -p -t {pane} '#{{cursor_x}} #{{cursor_y}}'",
        )
        client._feed = original
        assert await _until(lambda: b"DONE-42" in rec.out.get(pane, b""))
        live = bytes(rec.out[pane][marks[0] :]).decode()
        cx, cy = map(int, cursor.text()[0].split())
        lines = history.text() + screen.text()[: cy + 1]
        # Replay the live stream from the captured cursor, as a terminal would:
        # the cursor may sit after a carriage return on a written row.
        current, x = list(lines.pop()), cx
        for ch in live:
            if ch == "\r":
                x = 0
            elif ch == "\n":
                lines.append("".join(current))
                current, x = [], 0
            else:
                current[x : x + 1] = [ch]
                x += 1
        lines.append("".join(current))
        text = "\n".join(lines)
        numbers = [line for line in text.split("\n") if line.isdigit()]
        assert numbers == [str(i) for i in range(1, n + 1)]
    finally:
        await client.close()


@needs_tmux
@pytest.mark.asyncio
async def test_captured_protocol_lookalikes_stay_content(server):
    socket, env, _, _ = server
    client, _ = await _attach(socket, env)
    try:
        pane = await _pane(client)
        lines = ["%end 1 2 1", "%error 3 4 1", "%begin 5 6 1", "%output %0 x", "%exit"]
        script = "; ".join(f"echo {quote(line)}" for line in lines)
        await _run_in_pane(client, pane, f"clear; {script}")
        await asyncio.sleep(0.5)
        captured, following = await client.command(
            f"capture-pane -p -t {pane}", "display -p after"
        )
        assert captured.ok
        assert [line for line in captured.text() if line.startswith("%")] == lines
        assert following.text() == ["after"]
    finally:
        await client.close()


@needs_tmux
@pytest.mark.asyncio
async def test_window_and_session_notifications(server):
    socket, env, tmux, _ = server
    client, rec = await _attach(socket, env)
    try:
        tmux("new-window", "-d", "-t", "t:", "-n", "two", "/bin/sh")
        assert await _until(lambda: any(n == "window-add" for n, _ in rec.notes))
        tmux("new-session", "-d", "-s", "other", "/bin/sh")
        await client.run("switch-client -t other")
        assert await _until(
            lambda: any(
                n == "session-changed" and r.endswith(b" other") for n, r in rec.notes
            )
        )
        await client.run("refresh-client -B 'st:@*:#{@agent_state}'")
        win = tmux("display", "-p", "-t", "other", "#{window_id}").strip()
        tmux("set", "-w", "-t", win, "@agent_state", "busy")
        assert await _until(
            lambda: any(
                n == "subscription-changed" and r.endswith(b": busy")
                for n, r in rec.notes
            )
        )
    finally:
        await client.close()
