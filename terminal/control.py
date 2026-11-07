"""A tmux control-mode client (``tmux -C``) owned by the event loop.

The native terminal attaches to tmux through control mode: tmux stops drawing
and streams each pane's raw output as ``%output`` notifications, and takes
ordinary commands on stdin. This module is the protocol layer only: it spawns
the client on pipes, splits its stdout into command replies and
notifications, and unescapes pane output to exact bytes. The WebSocket bridge
built on it lives in ``terminal/native.py``.

Facts this module relies on, each pinned by ``tests/unit/test_terminal_control.py``
on an isolated tmux server:

- Every command line produces one ``%begin``/``%end`` (or ``%error``) block per
  command in it: ``a ; b`` yields two blocks, except that an ``%error`` skips
  the rest of its line with no block for them. A block whose ``%begin`` carries
  flags ``1`` answers a command this client sent; flags ``0`` is a command tmux
  ran on the client's behalf (a key binding fired by ``send-keys -K``), which
  nobody awaits.
- ``%output`` and the command blocks share one ordered stream: bytes reported
  before a block's ``%begin`` were in the pane's grid when the command ran,
  bytes after its ``%end`` were not. That is what makes a ``capture-pane``
  followed by the live stream exact, with nothing lost or doubled.
- ``%output`` escapes every byte below 0x20 and the backslash as a three-digit
  octal ``\\ooo``; every other byte is raw. Since a raw backslash never occurs,
  ``codecs.escape_decode`` restores the exact bytes in C.
- The client reads pipes, never a PTY, so no thread can block in a PTY syscall
  and it has no ``client_tty``.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from terminal.pty_bridge import terminate_client

logger = logging.getLogger("merlin.terminal")

# The stdout pipe is read in chunks of this size. The transport stops reading
# while more than READ_HIGH_WATER bytes wait to be parsed, so a consumer that
# falls behind makes tmux buffer (and pause the pane, see pause-after) instead
# of this process growing without bound.
READ_HIGH_WATER = 4 * 1024 * 1024

# A reply that never comes (a wedged server) must not hang a caller forever.
COMMAND_TIMEOUT = 15.0


class ControlError(Exception):
    """A command failed (``%error``), or the client is gone."""


@dataclass
class Reply:
    ok: bool
    lines: list[bytes] = field(default_factory=list)

    def text(self) -> list[str]:
        return [line.decode("utf-8", "replace") for line in self.lines]


def unescape(value: bytes) -> bytes:
    """Exact pane bytes from a ``%output`` value (octal escapes only)."""
    return codecs.escape_decode(value)[0]


def quote(arg: str) -> str:
    """Quote one argument for a tmux command line: single quotes, with any
    single quote closed, escaped and reopened. tmux's parser treats the
    content of single quotes literally, ``;`` and ``#`` included."""
    return "'" + arg.replace("'", "'\\''") + "'"


def hex_keys(data: bytes) -> str:
    """Bytes as the hex arguments of ``send-keys -H``: every byte reaches the
    pane exactly as given, none read as a key name."""
    return data.hex(" ")


class ControlClient:
    """One ``tmux -C`` client on pipes.

    ``on_output(pane_id, data)`` receives the exact bytes of every ``%output``
    and ``%extended-output``; ``on_notification(name, args)`` every other
    notification (``name`` without the ``%``, ``args`` the raw rest of the
    line). Both run on the event loop, in stream order, and must not block.
    """

    def __init__(
        self,
        on_output: Callable[[str, bytes], None],
        on_notification: Callable[[str, bytes], None],
    ) -> None:
        self._on_output = on_output
        self._on_notification = on_notification
        self._proc: asyncio.subprocess.Process | None = None
        # One entry per command line sent, in order: the futures of its
        # commands, answered front to back.
        self._pending: deque[list[asyncio.Future[Reply]]] = deque()
        self._block: list[bytes] | None = None
        self._block_ours = False
        self._started: asyncio.Future[None] | None = None
        self._reader: asyncio.Task | None = None
        self._closed = False
        self.exited = asyncio.Event()
        self.exit_reason = ""

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    async def start(
        self, argv: Sequence[str], env: Mapping[str, str], cwd: str | None = None
    ) -> None:
        """Spawn ``argv`` (a ``tmux ... -C attach|new-session ...`` command)
        and wait for its first block, the reply to the attach itself."""
        loop = asyncio.get_running_loop()
        self._started = loop.create_future()
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=dict(env),
            cwd=cwd,
            limit=READ_HIGH_WATER,
        )
        self._reader = asyncio.create_task(self._read_loop())
        done, _ = await asyncio.wait(
            {self._started, asyncio.ensure_future(self.exited.wait())},
            timeout=COMMAND_TIMEOUT,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not self._started.done():
            await self.close()
            raise ControlError(self.exit_reason or "tmux control client did not start")

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        stdout = self._proc.stdout
        try:
            while True:
                try:
                    line = await stdout.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    break
                except asyncio.LimitOverrunError as e:
                    # A line longer than the buffer: take it whole.
                    line = await stdout.readexactly(e.consumed)
                    line += await stdout.readuntil(b"\n")
                self._feed(line[:-1])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("tmux control stream failed")
        finally:
            self._fail_pending("tmux control client exited")
            self.exited.set()

    def _feed(self, line: bytes) -> None:
        if self._block is not None:
            if line.startswith((b"%end ", b"%error ")):
                ok = line.startswith(b"%end ")
                block, ours = self._block, self._block_ours
                self._block = None
                if self._started is not None and not self._started.done():
                    self._started.set_result(None)
                    return
                if ours and self._pending:
                    group = self._pending[0]
                    fut = group.pop(0)
                    if not fut.done():
                        fut.set_result(Reply(ok, block))
                    if not ok:
                        # tmux runs nothing after a failed command of a line.
                        for skipped in group:
                            if not skipped.done():
                                skipped.set_result(
                                    Reply(False, [b"skipped after an error"])
                                )
                        group.clear()
                    if not group:
                        self._pending.popleft()
                return
            self._block.append(line)
            return
        if line.startswith(b"%begin "):
            self._block = []
            self._block_ours = line.rsplit(b" ", 1)[-1] == b"1"
            return
        if line.startswith(b"%output "):
            _, pane, value = (line.split(b" ", 2) + [b""])[:3]
            self._on_output(pane.decode(), unescape(value))
            return
        if line.startswith(b"%extended-output "):
            head, _, value = line.partition(b" : ")
            parts = head.split(b" ")
            if len(parts) >= 2:
                self._on_output(parts[1].decode(), unescape(value))
            return
        if line.startswith(b"%"):
            name, _, rest = line[1:].partition(b" ")
            if name == b"exit":
                self.exit_reason = rest.decode("utf-8", "replace")
            self._on_notification(name.decode("ascii", "replace"), rest)
            return
        # Anything else outside a block is noise (it should not happen).

    def _fail_pending(self, reason: str) -> None:
        while self._pending:
            for fut in self._pending.popleft():
                if not fut.done():
                    fut.set_exception(ControlError(reason))

    async def command(
        self, *commands: str, timeout: float = COMMAND_TIMEOUT
    ) -> list[Reply]:
        """Run commands as one line (``a ; b``), so tmux runs them back to back
        with no pane output parsed in between; one reply per command. After a
        failed command, the ones after it in the line did not run and their
        replies say so."""
        if (
            self._closed
            or self._proc is None
            or self._proc.stdin is None
            or self.exited.is_set()
        ):
            raise ControlError("tmux control client is closed")
        loop = asyncio.get_running_loop()
        futures = [loop.create_future() for _ in commands]
        self._pending.append(list(futures))
        line = " ; ".join(commands).encode("utf-8") + b"\n"
        try:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()
        except (ConnectionError, OSError) as e:
            self._fail_pending("tmux control client is gone")
            raise ControlError(str(e)) from e
        return list(await asyncio.wait_for(asyncio.gather(*futures), timeout))

    async def run(self, command: str) -> Reply:
        """One command; raises ControlError on ``%error``."""
        (reply,) = await self.command(command)
        if not reply.ok:
            raise ControlError(b" ".join(reply.lines).decode("utf-8", "replace"))
        return reply

    async def close(self) -> None:
        """Detach this client (closing its stdin), never the session, and
        reap it, escalating to SIGHUP then SIGKILL if it lingers."""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 2.0)
        except asyncio.TimeoutError:
            await terminate_client(proc.pid)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 1.0)
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        self._fail_pending("tmux control client closed")


def control_env(base: Mapping[str, str]) -> dict[str, str]:
    """The environment for the control client process itself: no ``TMUX``
    (it would refuse to nest), nothing else changed. What the panes inherit is
    the server's business (``terminal_process_env`` when it is created)."""
    env = {k: v for k, v in base.items() if k not in ("TMUX", "TMUX_PANE")}
    env.setdefault("TERM", "xterm-256color")
    return env


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
