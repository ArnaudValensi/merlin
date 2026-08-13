"""Tests for the shared tmux reconnect policy."""

import contextlib
import os
import pty
import shutil
import subprocess
import time

import pytest

from board import sweep as board_sweep
from board.sweep import TmuxSession
from terminal.tmux import DEFAULT_SESSION_NAME, reconnect_argv


def _session(name: str = "renamed") -> TmuxSession:
    return TmuxSession(
        name=name,
        session_id="$0",
        attached=False,
        windows=1,
        activity=0,
    )


def test_reconnect_attaches_when_a_session_exists():
    assert reconnect_argv([_session()]) == ["tmux", "attach"]


def test_reconnect_bootstraps_when_no_session_exists():
    assert reconnect_argv([]) == [
        "tmux",
        "new-session",
        "-A",
        "-s",
        DEFAULT_SESSION_NAME,
        "-x",
        "120",
        "-y",
        "40",
    ]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_reconnect_follows_a_renamed_session(monkeypatch, tmp_path):
    """A reconnect attaches to the renamed session without creating another."""
    socket = tmp_path / "tmux.sock"

    def tmux(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-S", str(socket), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def private_capture(args: list[str]) -> str | None:
        result = tmux(*args)
        return result.stdout if result.returncode == 0 else None

    monkeypatch.setattr(board_sweep, "_tmux_capture", private_capture)
    created = tmux("new-session", "-d", "-s", DEFAULT_SESSION_NAME)
    assert created.returncode == 0, created.stderr
    before = len(board_sweep.run_session_sweep())
    renamed = tmux("rename-session", "-t", DEFAULT_SESSION_NAME, "renamed-by-user")
    assert renamed.returncode == 0, renamed.stderr

    args = reconnect_argv(board_sweep.run_session_sweep())
    private_args = [args[0], "-S", str(socket), *args[1:]]
    pid, master_fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env.pop("TMUX", None)
        env["TERM"] = "xterm-256color"
        os.execvpe(private_args[0], private_args, env)
        os._exit(1)

    try:
        deadline = time.monotonic() + 5
        current = None
        while time.monotonic() < deadline:
            current = tmux("list-clients", "-F", "#{client_session}").stdout.strip()
            if current:
                break
            time.sleep(0.05)

        assert len(board_sweep.run_session_sweep()) == before
        assert current == "renamed-by-user"
    finally:
        tmux("kill-server")
        with contextlib.suppress(OSError):
            os.close(master_fd)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
