"""Tests for the shared tmux reconnect policy."""

import contextlib
import os
import pty
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from board import sweep as board_sweep
from board.sweep import TmuxSession
from terminal.tmux import (
    DEFAULT_SESSION_NAME,
    SessionIdentity,
    parse_session_identity,
    reconnect_argv,
)

TMUX_CONF = Path(__file__).resolve().parents[2] / "terminal" / "tmux.conf"


def _session(
    name: str = "renamed", session_id: str = "$0", created: int = 1
) -> TmuxSession:
    return TmuxSession(
        name=name,
        session_id=session_id,
        created=created,
        attached=False,
        windows=1,
        activity=0,
    )


def test_reconnect_attaches_when_a_session_exists():
    assert reconnect_argv([_session()]) == ["tmux", "attach"]


def test_reconnect_attaches_directly_to_a_matching_identity():
    sessions = [_session("alpha", "$0", 10), _session("beta", "$1", 20)]

    assert reconnect_argv(sessions, SessionIdentity("$0", 10)) == [
        "tmux",
        "attach",
        "-t",
        "$0",
    ]


def test_reconnect_does_not_match_id_with_a_different_creation_time():
    sessions = [_session("replacement", "$0", 20)]

    assert reconnect_argv(sessions, SessionIdentity("$0", 10)) == [
        "tmux",
        "attach",
    ]


def test_reconnect_falls_back_when_the_preferred_session_was_deleted():
    sessions = [_session("survivor", "$1", 20)]

    assert reconnect_argv(sessions, SessionIdentity("$0", 10)) == [
        "tmux",
        "attach",
    ]


@pytest.mark.parametrize(
    ("session_id", "created"),
    [
        (None, None),
        ("", "1"),
        ("alpha", "1"),
        ("$1", ""),
        ("$1", "not-a-number"),
        ("$1", "0"),
        ("$-1", "1"),
    ],
)
def test_parse_session_identity_rejects_missing_or_malformed_values(
    session_id, created
):
    assert parse_session_identity(session_id, created) is None


def test_parse_session_identity_accepts_tmux_id_and_creation_time():
    assert parse_session_identity("$12", "1699999900") == SessionIdentity(
        "$12", 1699999900
    )


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
def test_toolbar_new_window_inherits_current_cwd(tmp_path):
    """The toolbar's F2 sequence opens the new window in the active pane's cwd."""
    socket = tmp_path / "tmux.sock"
    home = tmp_path / "home"
    session_root = tmp_path / "session-root"
    project = tmp_path / "project"
    home.mkdir()
    (home / ".zshrc").touch()  # Skip zsh's first-run prompt in the test pane.
    session_root.mkdir()
    project.mkdir()
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.update({"HOME": str(home), "TERM": "xterm-256color"})

    def tmux(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-S", str(socket), "-f", str(TMUX_CONF), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    created = tmux(
        "new-session",
        "-d",
        "-s",
        "toolbar",
        "-c",
        str(session_root),
        "-x",
        "120",
        "-y",
        "40",
    )
    assert created.returncode == 0, created.stderr

    pid = None
    master_fd = None
    try:
        tmux("send-keys", "-t", "toolbar", "-l", f"cd {shlex.quote(str(project))}")
        tmux("send-keys", "-t", "toolbar", "Enter")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            cwd = tmux(
                "display-message", "-p", "-t", "toolbar", "#{pane_current_path}"
            ).stdout.strip()
            if os.path.realpath(cwd) == os.path.realpath(project):
                break
            time.sleep(0.05)
        else:
            pytest.fail("active pane did not change to the project directory")

        pid, master_fd = pty.fork()
        if pid == 0:
            os.execvpe(
                "tmux",
                ["tmux", "-S", str(socket), "attach", "-t", "toolbar"],
                env,
            )
            os._exit(1)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if tmux("list-clients").stdout.strip():
                break
            time.sleep(0.05)
        else:
            pytest.fail("tmux client did not attach")

        before = tmux(
            "list-windows", "-t", "toolbar", "-F", "#{window_id}"
        ).stdout.split()
        os.write(master_fd, b"\x1bOQ")  # xterm.js sends this for toolbar F2.

        after = before
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            after = tmux(
                "list-windows", "-t", "toolbar", "-F", "#{window_id}"
            ).stdout.split()
            if len(after) == len(before) + 1:
                break
            time.sleep(0.05)

        assert len(after) == len(before) + 1
        cwd = tmux(
            "display-message", "-p", "-t", "toolbar", "#{pane_current_path}"
        ).stdout.strip()
        assert os.path.realpath(cwd) == os.path.realpath(project)
    finally:
        tmux("kill-server")
        if master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        if pid is not None:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_reconnect_follows_a_renamed_session(monkeypatch, tmp_path):
    """A pinned reconnect follows a rename even when another session exists."""
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
    original = board_sweep.run_session_sweep()[0]
    renamed = tmux("rename-session", "-t", DEFAULT_SESSION_NAME, "renamed-by-user")
    assert renamed.returncode == 0, renamed.stderr
    other = tmux("new-session", "-d", "-s", "other-session")
    assert other.returncode == 0, other.stderr
    before = len(board_sweep.run_session_sweep())

    identity = SessionIdentity(original.session_id, original.created)
    args = reconnect_argv(board_sweep.run_session_sweep(), identity)
    assert args == ["tmux", "attach", "-t", original.session_id]
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


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_reconnect_rejects_an_id_reused_after_server_restart(monkeypatch, tmp_path):
    """An old $0 must not identify the new server's unrelated $0 session."""
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
    try:
        created = tmux("new-session", "-d", "-s", "old-server-session")
        assert created.returncode == 0, created.stderr
        (old,) = board_sweep.run_session_sweep()
        stopped = tmux("kill-server")
        assert stopped.returncode == 0, stopped.stderr

        while int(time.time()) <= old.created:
            time.sleep(0.05)

        created = tmux("new-session", "-d", "-s", "replacement-session")
        assert created.returncode == 0, created.stderr
        (replacement,) = board_sweep.run_session_sweep()
        assert replacement.session_id == old.session_id == "$0"
        assert replacement.created != old.created

        identity = SessionIdentity(old.session_id, old.created)
        assert reconnect_argv([replacement], identity) == ["tmux", "attach"]
    finally:
        tmux("kill-server")
