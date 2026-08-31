"""Container-side SSH server for Merlin SaaS.

Runs a lightweight asyncssh server on 127.0.0.1:2222 (localhost only).
No authentication — only reachable via the portal's authenticated tunnel.

SSH is the raw machine: a login shell, file transfer, system access. The
session layer (tmux, agents, continuity across devices) is what the web
terminal is for. Someone who wants the Merlin session over SSH asks for it
explicitly with ``ssh <env> -t tmux attach``.

Features:
- Interactive sessions run the account's login shell (no tmux)
- SFTP with full filesystem access
- Ed25519 host key auto-generated and persisted at ~/.merlin/ssh/host_key
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import pwd
import signal
import struct
import termios
from pathlib import Path

import asyncssh

from terminal.tmux import terminal_process_env

logger = logging.getLogger("merlin.ssh")

# Module-level state for cleanup
_server: asyncssh.SSHAcceptor | None = None


def _host_key_path() -> Path:
    """Path for the persistent host key."""
    try:
        import paths

        return paths.data_dir() / "ssh" / "host_key"
    except ImportError:
        return Path.home() / ".merlin" / "ssh" / "host_key"


def _ensure_host_key() -> str:
    """Load or generate the Ed25519 host key. Returns the file path as string."""
    key_path = _host_key_path()
    if key_path.exists():
        logger.info("SSH host key loaded from %s", key_path)
        return str(key_path)

    # Generate and persist
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = asyncssh.generate_private_key("ssh-ed25519")
    key_path.write_bytes(key.export_private_key())
    key_path.chmod(0o600)
    logger.info("SSH host key generated at %s", key_path)
    return str(key_path)


class MerlinShellServer(asyncssh.SSHServer):
    """SSH server with no auth (localhost only, behind portal tunnel)."""

    def begin_auth(self, username: str) -> bool:
        # No authentication required — return False to accept immediately
        return False


def _has_terminfo(term: str) -> bool:
    """Check if a terminfo entry exists for the given TERM value."""
    import curses

    try:
        curses.setupterm(term)
        return True
    except curses.error:
        return False


def _set_winsize(fd: int, width: int, height: int) -> None:
    """Set terminal window size on a file descriptor."""
    winsize = struct.pack("HHHH", height, width, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def login_shell_argv() -> list[str]:
    """The account's login shell, resolved the way OpenSSH resolves it.

    The passwd entry is authoritative for the account. ``$SHELL`` only reflects
    whatever environment the Merlin daemon happened to be started from, which is
    not the same thing and is often wrong inside a container, so it is a
    fallback rather than the first choice. ``-l`` makes it a login shell, so
    profile files load and the session behaves like any other SSH login.
    """
    shell = ""
    with contextlib.suppress(KeyError):  # uid with no passwd entry
        shell = pwd.getpwuid(os.getuid()).pw_shell or ""
    shell = shell or os.environ.get("SHELL", "") or "/bin/sh"
    return [shell, "-l"]


async def _handle_session(process: asyncssh.SSHServerProcess) -> None:
    """Process factory: run a command, or drop the user in a login shell.

    Deliberately no tmux. SSH is the raw machine; the web terminal owns the
    session layer. See the module docstring.
    """
    command = process.command
    if command:
        process_args = ["/bin/sh", "-c", command]
    else:
        process_args = login_shell_argv()

    term_type = process.get_terminal_type() or "xterm-256color"
    # Fall back if the client's TERM isn't in the container's terminfo.
    # Common case: Kitty sends xterm-kitty which most containers lack.
    if not _has_terminfo(term_type):
        logger.info(
            "TERM=%s not in terminfo, falling back to xterm-256color", term_type
        )
        term_type = "xterm-256color"
    term_size = process.get_terminal_size()

    master_fd, slave_fd = os.openpty()
    proc: asyncio.subprocess.Process | None = None
    reader_registered = False
    write_task: asyncio.Task[None] | None = None
    loop = asyncio.get_event_loop()

    try:
        if term_size:
            _set_winsize(slave_fd, term_size[0], term_size[1])

        env = terminal_process_env(os.environ, term=term_type)

        proc = await asyncio.create_subprocess_exec(
            *process_args,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1

        os.set_blocking(master_fd, False)

        # Handle terminal resize from SSH client.
        # Expected: EBADF after master_fd is closed during teardown —
        # the SSH client can send a resize after the process exits.
        def _on_resize(w: int, h: int, pw: int, ph: int) -> None:
            try:
                _set_winsize(master_fd, w, h)
            except OSError as e:
                if e.errno == errno.EBADF:
                    logger.debug("Resize after PTY close (expected)")
                else:
                    logger.warning("Unexpected error on PTY resize: %s", e)

        # Hook terminal-resize events to update the PTY window size. asyncssh
        # exposes this as an overridable attribute, not a documented method.
        process.terminal_size_changed = _on_resize  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

        # PTY master → SSH client
        done = asyncio.Event()

        def on_pty_readable() -> None:
            try:
                data = os.read(master_fd, 65536)
                if data:
                    try:
                        process.stdout.write(data.decode("utf-8", errors="replace"))
                    except TypeError:
                        process.stdout.write(data)
                else:
                    done.set()
            except OSError as e:
                # EIO is expected when the slave side closes (process exit).
                # EBADF means fd was already closed during teardown.
                if e.errno not in (errno.EIO, errno.EBADF):
                    logger.warning("Unexpected PTY read error: %s", e)
                done.set()

        loop.add_reader(master_fd, on_pty_readable)
        reader_registered = True

        # SSH client → PTY master
        async def ssh_to_pty() -> None:
            try:
                while not done.is_set():
                    data = await process.stdin.read(65536)
                    if not data:
                        break
                    if isinstance(data, str):
                        data = data.encode("utf-8")
                    try:
                        os.write(master_fd, data)
                    except BlockingIOError:
                        # PTY write buffer full — yield and retry once
                        await asyncio.sleep(0.01)
                        try:
                            os.write(master_fd, data)
                        except BlockingIOError:
                            logger.debug("PTY write buffer still full, dropping data")
                        except OSError as e:
                            if e.errno in (errno.EIO, errno.EBADF):
                                break
                            raise
                    except OSError as e:
                        if e.errno in (errno.EIO, errno.EBADF):
                            break
                        raise
            except asyncssh.BreakReceived:
                logger.debug("SSH break received in write loop")

        write_task = asyncio.create_task(ssh_to_pty())

        await proc.wait()

        process.exit(proc.returncode or 0)

    except asyncssh.BreakReceived:
        logger.info("SSH session interrupted by break signal")
        process.exit(1)
    finally:
        # Clean up reader before closing fd
        if reader_registered:
            loop.remove_reader(master_fd)

        # Cancel write task
        if write_task is not None:
            write_task.cancel()
            try:
                await write_task
            except asyncio.CancelledError:
                pass

        # Reap subprocess — send SIGHUP to process group, then wait.
        # ProcessLookupError (ESRCH): process already exited — expected.
        # PermissionError (EPERM): shouldn't happen (we spawned it) — log.
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGHUP)
            except ProcessLookupError:
                logger.debug("Process already exited before SIGHUP")
            except PermissionError:
                logger.warning("No permission to SIGHUP process group %d", proc.pid)

            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("Process did not exit after SIGHUP, sending SIGKILL")
                proc.kill()
                await proc.wait()

        # Close file descriptors.
        # EBADF on close means double-close — a bug, not expected. Log it.
        if slave_fd >= 0:
            os.close(slave_fd)
        try:
            os.close(master_fd)
        except OSError as e:
            if e.errno == errno.EBADF:
                logger.debug("master_fd already closed")
            else:
                logger.warning("Error closing master PTY fd: %s", e)


async def start_ssh_server(port: int | None = None) -> asyncssh.SSHAcceptor | None:
    """Start the SSH server on 127.0.0.1.

    Args:
        port: Port to listen on. Defaults to SSH_PORT env var or 2222.

    Returns:
        The server acceptor for cleanup, or None if startup failed.
    """
    global _server

    if port is None:
        port = int(os.environ.get("SSH_PORT", "2222"))

    try:
        host_key_path = _ensure_host_key()
    except Exception:
        logger.exception("Failed to load/generate SSH host key")
        return None

    try:
        _server = await asyncssh.create_server(
            MerlinShellServer,
            host="127.0.0.1",
            port=port,
            server_host_keys=[host_key_path],
            process_factory=_handle_session,
            sftp_factory=asyncssh.SFTPServer,
        )
        logger.info("SSH server listening on 127.0.0.1:%d", port)
        return _server
    except OSError as e:
        logger.warning("SSH server failed to start on port %d: %s", port, e)
        return None
    except Exception:
        logger.exception("SSH server unexpected error during startup")
        return None


async def stop_ssh_server() -> None:
    """Stop the SSH server if running."""
    global _server
    if _server is not None:
        _server.close()
        await _server.wait_closed()
        _server = None
        logger.info("SSH server stopped")
