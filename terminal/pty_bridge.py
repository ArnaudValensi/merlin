"""Non-blocking PTY bridge — event-loop I/O on a PTY master fd.

Why this exists: the terminal WebSocket used to read the PTY with a
blocking os.read() in an executor thread. On macOS, os.close() on a PTY
fd while another thread is blocked in read() on the same fd deadlocks
both threads in the kernel: the process enters uninterruptible sleep and
even SIGKILL cannot reap it. One terminal disconnect could freeze the
whole event loop, and with it the SaaS tunnel (2026-07-02 outage).

The fix is structural: the master fd is non-blocking and all I/O goes
through event-loop readiness callbacks (loop.add_reader / add_writer),
so no thread is ever parked inside a PTY syscall and teardown can never
deadlock, on any platform. add_reader/add_writer are used instead of
asyncio pipe transports because they are loop-agnostic: uvloop (which
uvicorn auto-selects when installed) supports them on any fd, while its
pipe transports reject TTYs.
"""

import asyncio
import logging
import os
import signal
from collections import deque

logger = logging.getLogger("merlin.terminal")

# One os.read per readiness callback; the loop re-fires while data remains.
READ_CHUNK = 65536

# Pause reading the PTY when this much output is buffered and the consumer
# has not caught up. The kernel PTY buffer then blocks the child: real
# backpressure instead of unbounded memory growth on a slow WebSocket.
HIGH_WATER = 256 * 1024

# A write that cannot make progress for this long means the child stopped
# draining its side. Give up instead of waiting forever.
WRITE_STALL_TIMEOUT = 30.0

# Grace periods for terminate_client(): SIGHUP, then SIGKILL.
SIGHUP_GRACE = 2.0
SIGKILL_GRACE = 1.0
_REAP_POLL = 0.05


class PtyBridge:
    """Async reader/writer over a PTY master fd, owned by the event loop.

    Single-consumer: exactly one task may call read() at a time (the
    terminal WebSocket handler). write() may be called from any number of
    tasks; writes are serialized by an internal lock. close() is
    synchronous and idempotent, and wakes any waiting reader or writer.
    """

    def __init__(
        self,
        master_fd: int,
        *,
        high_water: int = HIGH_WATER,
        write_stall_timeout: float = WRITE_STALL_TIMEOUT,
    ) -> None:
        self._fd = master_fd
        self._loop = asyncio.get_running_loop()
        self._high_water = high_water
        self._write_stall_timeout = write_stall_timeout

        self._chunks: deque[bytes] = deque()
        self._buffered = 0
        self._eof = False
        self._closed = False
        self._reading = False
        self._read_waiter: asyncio.Future[None] | None = None
        self._write_waiter: asyncio.Future[None] | None = None
        self._write_lock = asyncio.Lock()

        os.set_blocking(master_fd, False)
        self._start_reading()

    @property
    def fd(self) -> int:
        """The master fd, for ioctl (window resize). Do not read/write it."""
        return self._fd

    @property
    def closed(self) -> bool:
        return self._closed

    # -- reading ------------------------------------------------------------

    def _start_reading(self) -> None:
        if not self._reading and not self._closed and not self._eof:
            self._loop.add_reader(self._fd, self._on_readable)
            self._reading = True

    def _stop_reading(self) -> None:
        if self._reading:
            try:
                self._loop.remove_reader(self._fd)
            except (OSError, ValueError):
                pass
            self._reading = False

    def _on_readable(self) -> None:
        try:
            data = os.read(self._fd, READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            # EIO: the slave side is gone. The PTY's way of saying EOF.
            data = b""
        if not data:
            self._eof = True
            self._stop_reading()
            self._wake_reader()
            return
        self._chunks.append(data)
        self._buffered += len(data)
        if self._buffered >= self._high_water:
            self._stop_reading()
        self._wake_reader()

    def _wake_reader(self) -> None:
        waiter = self._read_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def read(self) -> bytes:
        """Next chunk of PTY output; b"" once the PTY is gone or closed."""
        while True:
            if self._chunks:
                data = self._chunks.popleft()
                self._buffered -= len(data)
                if self._buffered < self._high_water:
                    self._start_reading()
                return data
            if self._eof or self._closed:
                return b""
            waiter = self._loop.create_future()
            self._read_waiter = waiter
            try:
                await waiter
            finally:
                self._read_waiter = None

    # -- writing ------------------------------------------------------------

    async def write(self, data: bytes) -> bool:
        """Write all of data to the PTY.

        Returns False if the PTY is closed, the child is gone, or the
        write stalled longer than the stall timeout (child not draining).
        Never blocks the event loop and never raises for PTY-side errors.
        """
        async with self._write_lock:
            view = memoryview(data)
            while view:
                if self._closed:
                    return False
                try:
                    written = os.write(self._fd, view)
                    view = view[written:]
                    continue
                except BlockingIOError:
                    pass
                except OSError:
                    return False
                if not await self._wait_writable():
                    return False
            return True

    async def _wait_writable(self) -> bool:
        waiter: asyncio.Future[None] = self._loop.create_future()

        def _ready() -> None:
            if not waiter.done():
                waiter.set_result(None)

        try:
            self._loop.add_writer(self._fd, _ready)
        except (OSError, ValueError):
            return False
        self._write_waiter = waiter
        try:
            await asyncio.wait_for(waiter, self._write_stall_timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "PTY write stalled for %.0fs, giving up (fd=%d)",
                self._write_stall_timeout,
                self._fd,
            )
            return False
        except asyncio.CancelledError:
            if self._closed:
                # close() cancelled us; report failure, not cancellation.
                return False
            raise
        finally:
            self._write_waiter = None
            try:
                self._loop.remove_writer(self._fd)
            except (OSError, ValueError):
                pass

    # -- teardown -----------------------------------------------------------

    def close(self) -> None:
        """Detach from the event loop and close the fd. Sync, idempotent.

        Safe by construction: no thread can be inside a PTY syscall on
        this fd, so close() cannot deadlock.
        """
        if self._closed:
            return
        self._closed = True
        self._stop_reading()
        try:
            self._loop.remove_writer(self._fd)
        except (OSError, ValueError):
            pass
        if self._write_waiter is not None and not self._write_waiter.done():
            self._write_waiter.cancel()
        self._wake_reader()
        try:
            os.close(self._fd)
        except OSError:
            pass


# -- child process teardown ---------------------------------------------


def _try_reap(pid: int) -> bool:
    """Non-blocking waitpid. True if the child is gone (or already reaped)."""
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return reaped != 0


async def _wait_exit(pid: int, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if _try_reap(pid):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(_REAP_POLL)


async def terminate_client(
    pid: int,
    *,
    sighup_grace: float = SIGHUP_GRACE,
    sigkill_grace: float = SIGKILL_GRACE,
) -> None:
    """SIGHUP the tmux client and make sure it actually exits.

    pid is the tmux *client* attached to the persistent session; the
    session lives in the tmux server and survives. Escalates to SIGKILL
    after a grace period so a wedged client can never keep the PTY slave
    open (stuck clients were part of the 2026-07-02 incident).
    """
    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        _try_reap(pid)
        return
    except OSError:
        return
    try:
        if await _wait_exit(pid, sighup_grace):
            return
        logger.warning("tmux client pid=%d ignored SIGHUP, sending SIGKILL", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            _try_reap(pid)
            return
        if await _wait_exit(pid, sigkill_grace):
            return
        logger.error("tmux client pid=%d survived SIGKILL", pid)
    except asyncio.CancelledError:
        # Handler cancelled (e.g. server shutdown): best-effort SIGKILL so
        # the client cannot outlive us, then propagate the cancellation.
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        _try_reap(pid)
        raise
