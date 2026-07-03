"""Tests for the non-blocking PTY bridge (terminal/pty_bridge.py).

The bridge exists so that no thread is ever blocked inside a PTY
syscall (blocking read + concurrent close deadlocks in the macOS
kernel). These tests pin down the behaviors that make that guarantee
useful: EOF detection, backpressure, write stalls, and teardown that
always completes.
"""

import asyncio
import os
import pty
import signal
import time
import tty

import pytest

from terminal.pty_bridge import PtyBridge, terminate_client


def _open_raw_pty():
    """PTY pair in raw mode (no echo, no canonical line buffering)."""
    master, slave = pty.openpty()
    tty.setraw(slave)
    return master, slave


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


class TestBridgeRead:
    def test_reads_slave_output(self):
        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master)
            try:
                os.write(slave, b"hello bridge")
                data = await asyncio.wait_for(bridge.read(), timeout=5)
                assert data == b"hello bridge"
            finally:
                bridge.close()
                os.close(slave)

        asyncio.run(run())

    def test_eof_when_slave_closes(self):
        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master)
            try:
                os.close(slave)
                data = await asyncio.wait_for(bridge.read(), timeout=5)
                assert data == b""
            finally:
                bridge.close()

        asyncio.run(run())

    def test_buffered_output_delivered_before_eof(self):
        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master)
            try:
                os.write(slave, b"last words")
                # Let the reader callback pick the data up, then close
                await asyncio.sleep(0.05)
                os.close(slave)
                chunks = []
                while True:
                    data = await asyncio.wait_for(bridge.read(), timeout=5)
                    if not data:
                        break
                    chunks.append(data)
                assert b"".join(chunks) == b"last words"
            finally:
                bridge.close()

        asyncio.run(run())

    def test_read_after_close_returns_empty(self):
        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master)
            bridge.close()
            os.close(slave)
            assert await bridge.read() == b""

        asyncio.run(run())

    def test_close_wakes_pending_reader(self):
        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master)
            reader = asyncio.create_task(bridge.read())
            await asyncio.sleep(0.05)
            bridge.close()
            data = await asyncio.wait_for(reader, timeout=5)
            assert data == b""
            os.close(slave)

        asyncio.run(run())

    def test_high_water_pauses_and_resumes(self):
        """Reading pauses at the high-water mark, resumes when drained,
        and no output is lost."""

        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master, high_water=1024)
            payload = bytes(range(256)) * 32  # 8 KB
            try:
                os.write(slave, payload)
                await asyncio.sleep(0.1)
                # Buffer is capped: reader detached at the high-water mark
                assert bridge._buffered >= 1024
                assert bridge._reading is False
                received = b""
                while len(received) < len(payload):
                    data = await asyncio.wait_for(bridge.read(), timeout=5)
                    assert data
                    received += data
                assert received == payload
            finally:
                bridge.close()
                os.close(slave)

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


class TestBridgeWrite:
    def test_write_reaches_slave(self):
        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master)
            try:
                assert await bridge.write(b"typed input") is True
                data = os.read(slave, 100)
                assert data == b"typed input"
            finally:
                bridge.close()
                os.close(slave)

        asyncio.run(run())

    def test_large_write_completes_with_slow_reader(self):
        """A write bigger than the kernel PTY buffer completes once the
        slave side drains, without blocking the event loop."""

        async def run():
            master, slave = _open_raw_pty()
            os.set_blocking(slave, False)
            bridge = PtyBridge(master)
            payload = b"x" * (256 * 1024)
            try:
                writer = asyncio.create_task(bridge.write(payload))
                received = 0
                while received < len(payload):
                    if writer.done() and not writer.result():
                        pytest.fail("write gave up before the slave drained")
                    try:
                        chunk = os.read(slave, 65536)
                        received += len(chunk)
                    except BlockingIOError:
                        await asyncio.sleep(0.01)
                assert await asyncio.wait_for(writer, timeout=5) is True
                assert received == len(payload)
            finally:
                bridge.close()
                os.close(slave)

        asyncio.run(run())

    def test_stalled_write_returns_false(self):
        """If the slave side never drains, write gives up after the stall
        timeout instead of waiting forever."""

        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master, write_stall_timeout=0.2)
            try:
                start = time.monotonic()
                ok = await asyncio.wait_for(
                    bridge.write(b"x" * (1024 * 1024)), timeout=5
                )
                elapsed = time.monotonic() - start
                assert ok is False
                assert elapsed < 3
            finally:
                bridge.close()
                os.close(slave)

        asyncio.run(run())

    def test_close_wakes_stalled_writer(self):
        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master, write_stall_timeout=30.0)
            writer = asyncio.create_task(bridge.write(b"x" * (1024 * 1024)))
            await asyncio.sleep(0.1)
            bridge.close()
            assert await asyncio.wait_for(writer, timeout=5) is False
            os.close(slave)

        asyncio.run(run())

    def test_write_after_close_returns_false(self):
        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master)
            bridge.close()
            os.close(slave)
            assert await bridge.write(b"late") is False

        asyncio.run(run())

    def test_write_to_dead_pty_never_hangs_or_raises(self):
        """All slave fds closed: the kernel may buffer or refuse the
        write (platform-dependent), but it must complete promptly and
        never raise. The reader's EOF is what triggers teardown."""

        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master)
            try:
                os.close(slave)
                await asyncio.sleep(0.05)
                ok = await asyncio.wait_for(bridge.write(b"into the void"), timeout=5)
                assert isinstance(ok, bool)
            finally:
                bridge.close()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


class TestBridgeClose:
    def test_close_is_idempotent(self):
        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master)
            bridge.close()
            bridge.close()  # must not raise
            assert bridge.closed is True
            os.close(slave)

        asyncio.run(run())

    def test_close_closes_master_fd(self):
        async def run():
            master, slave = _open_raw_pty()
            bridge = PtyBridge(master)
            bridge.close()
            with pytest.raises(OSError):
                os.fstat(master)
            os.close(slave)

        asyncio.run(run())


# ---------------------------------------------------------------------------
# tmux client termination
# ---------------------------------------------------------------------------


def _fork_child(setup):
    """Fork a child that runs setup() then sleeps. Never returns in child."""
    pid = os.fork()
    if pid == 0:
        try:
            setup()
            time.sleep(30)
        finally:
            os._exit(0)
    return pid


def _assert_reaped(pid):
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


class TestTerminateClient:
    def test_sighup_terminates_and_reaps(self):
        pid = _fork_child(lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL))
        asyncio.run(terminate_client(pid, sighup_grace=2.0, sigkill_grace=1.0))
        _assert_reaped(pid)

    def test_escalates_to_sigkill_when_sighup_ignored(self):
        pid = _fork_child(lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN))
        asyncio.run(terminate_client(pid, sighup_grace=0.2, sigkill_grace=2.0))
        _assert_reaped(pid)

    def test_already_exited_child_is_reaped(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        time.sleep(0.1)
        asyncio.run(terminate_client(pid))
        _assert_reaped(pid)

    def test_gone_pid_does_not_raise(self):
        # A child that exited and was already reaped: the pid no longer
        # exists. terminate_client must return without raising.
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        asyncio.run(terminate_client(pid))
