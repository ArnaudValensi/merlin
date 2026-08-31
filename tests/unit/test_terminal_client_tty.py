"""The platform contract behind per-client tmux switching.

The web terminal targets *this* browser's tmux client by tty. That tty is
derived from the PTY master the parent already holds (``os.ptsname``) rather
than looked up afterwards, which is what keeps a single code path on Linux and
macOS: the OS returns a native string (``/dev/pts/N`` vs ``/dev/ttysNNN``) and
tmux consumes it unchanged.

These tests pin the assumption that derivation and tmux agree. They run
identically on both platforms, so whichever one breaks the contract fails loudly
here instead of silently swallowing every session switch (which is exactly how
the macOS breakage went unnoticed: a ``/proc`` read returned "" and every caller
quietly did nothing).
"""

from __future__ import annotations

import contextlib
import os
import pty
import shutil
import subprocess
import time
from collections.abc import Iterator

import pytest


def test_derived_tty_is_a_terminal_path():
    """``os.ptsname`` on the master names the slave side, with no lookup."""
    master_fd, slave_fd = os.openpty()
    try:
        assert os.ptsname(master_fd) == os.ttyname(slave_fd)
    finally:
        os.close(master_fd)
        os.close(slave_fd)


@contextlib.contextmanager
def _attached_client(socket: str, session: str) -> Iterator[tuple[int, str]]:
    """Fork a real tmux client attached to ``session``, yielding (pid, tty).

    Registered for cleanup before anything can fail, so a failed assertion
    inside the ``with`` body still reaps the child and closes the master fd.
    """
    pid, master_fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child only execs
        try:
            os.execvp("tmux", ["tmux", "-S", socket, "attach", "-t", session])
        finally:
            # execvp raises rather than returning, so this is the only path
            # that keeps a failed exec from unwinding into pytest's machinery.
            os._exit(1)
    try:
        yield pid, os.ptsname(master_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(master_fd)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


@pytest.fixture
def tmux_server(tmp_path):
    """A private tmux server, always killed even if setup itself fails."""
    socket = str(tmp_path / "tmux.sock")

    def tmux(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-S", socket, *args],
            capture_output=True,
            text=True,
            check=False,
        )

    try:
        yield socket, tmux
    finally:
        tmux("kill-server")


def _wait_for(predicate, timeout: float = 5.0):
    """Poll ``predicate`` until it returns a truthy value, then return it."""
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return value


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_derived_tty_matches_tmux_and_switches_only_that_client(tmux_server):
    """The derived tty is tmux's ``client_tty``, and a switch moves only it."""
    socket, tmux = tmux_server

    created = tmux("-f", "/dev/null", "new-session", "-d", "-s", "first")
    assert created.returncode == 0, created.stderr
    second = tmux("-f", "/dev/null", "new-session", "-d", "-s", "second")
    assert second.returncode == 0, second.stderr

    # Two clients on the same session: the switch must move one and not the
    # other. That per-client isolation is the whole reason the tty matters.
    with _attached_client(socket, "first") as (pid, derived):
        with _attached_client(socket, "first") as (other_pid, other_tty):

            def both_attached():
                # Wait for *both* clients: a truthy-but-partial dict would let
                # the poll return after only the first client registered.
                listed = dict(
                    line.split("\t")
                    for line in tmux(
                        "list-clients", "-F", "#{client_pid}\t#{client_tty}"
                    ).stdout.splitlines()
                    if "\t" in line
                )
                return listed if len(listed) == 2 else None

            clients = _wait_for(both_attached)
            assert clients, "both tmux clients never attached"

            # Derivation agrees with tmux for both clients — this is the
            # assertion that would fail loudly on a platform where ptsname and
            # tmux disagree about the tty's name.
            assert clients[str(pid)] == derived
            assert clients[str(other_pid)] == other_tty

            switched = tmux("switch-client", "-c", derived, "-t", "second")
            assert switched.returncode == 0, switched.stderr

            def session_of(tty: str) -> str:
                return tmux(
                    "display-message", "-p", "-t", tty, "#{client_session}"
                ).stdout.strip()

            assert _wait_for(lambda: session_of(derived) == "second"), (
                "the targeted client did not move"
            )
            assert session_of(other_tty) == "first", (
                "the switch moved another browser tab's client"
            )
