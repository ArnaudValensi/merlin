"""Integration tests for the switcher's tmux command layer (board/sweep.py)
against a real, throwaway tmux server on a private socket. Skipped when tmux is
not installed. These cover the mutations that a pure parser test cannot:
switch-client per client, create-or-switch by directory, rename, kill, and
reading a client's current session.
"""

from __future__ import annotations

import os
import pty
import re
import shutil
import subprocess
import time

import pytest

from board import sweep

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)

_SOCK = "boardtest"


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", _SOCK, *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def tmux_server(monkeypatch, request):
    """A private tmux server with two detached sessions. Each test gets its OWN
    socket (``-L boardtest_<test>``) so tests can't contaminate each other under
    random ordering, and none touch the developer's real tmux."""
    global _SOCK
    _SOCK = "boardtest_" + re.sub(r"[^A-Za-z0-9]+", "_", request.node.name)
    real_run = subprocess.run

    def routed(cmd, *a, **kw):
        # Inject the private socket for bare board.sweep tmux calls; leave calls
        # that already name a socket (the _tmux helper) untouched.
        if isinstance(cmd, list) and cmd[:1] == ["tmux"] and "-L" not in cmd:
            cmd = ["tmux", "-L", _SOCK, *cmd[1:]]
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(sweep.subprocess, "run", routed)

    _tmux("kill-server")
    _tmux("new-session", "-d", "-s", "alpha")
    _tmux("new-session", "-d", "-s", "beta")
    try:
        yield
    finally:
        _tmux("kill-server")


def test_run_session_sweep_lists_sessions(tmux_server):
    names = {s.name for s in sweep.run_session_sweep()}
    assert {"alpha", "beta"} <= names


def test_create_or_get_creates_new(tmux_server, tmp_path):
    proj = tmp_path / "my-proj"
    proj.mkdir()
    name = sweep.create_or_get_session(str(proj))
    assert name == "my-proj"
    assert name in {s.name for s in sweep.run_session_sweep()}


def test_create_or_get_returns_existing(tmux_server, tmp_path):
    # Directory basename collides with an existing session -> reuse, not dup.
    d = tmp_path / "alpha"
    d.mkdir()
    before = len(sweep.run_session_sweep())
    name = sweep.create_or_get_session(str(d))
    assert name == "alpha"
    assert len(sweep.run_session_sweep()) == before  # no new session


def test_rename_session(tmux_server):
    assert sweep.rename_session("beta", "gamma") is True
    names = {s.name for s in sweep.run_session_sweep()}
    assert "gamma" in names and "beta" not in names


def test_kill_session(tmux_server):
    assert sweep.kill_session("beta") is True
    assert "beta" not in {s.name for s in sweep.run_session_sweep()}


def test_new_window(tmux_server):
    before = _tmux("list-windows", "-t", "alpha", "-F", "#{window_id}").stdout.split()
    wid = sweep.new_window("alpha")
    assert wid and wid.startswith("@")
    after = _tmux("list-windows", "-t", "alpha", "-F", "#{window_id}").stdout.split()
    assert len(after) == len(before) + 1
    assert wid in after


def test_new_window_inherits_active_window_cwd(tmux_server, tmp_path):
    """+ new window opens in the session's currently selected window's live cwd."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _tmux("new-session", "-d", "-s", "gamma", "-c", str(proj))
    wid = sweep.new_window("gamma")
    assert wid
    path = _tmux(
        "display-message", "-p", "-t", f"gamma:{wid}", "#{pane_current_path}"
    ).stdout.strip()
    assert os.path.realpath(path) == os.path.realpath(str(proj))


def test_reorder_windows(tmux_server):
    sweep.new_window("alpha")
    sweep.new_window("alpha")
    ids = _tmux("list-windows", "-t", "alpha", "-F", "#{window_id}").stdout.split()
    assert len(ids) == 3
    rev = list(reversed(ids))
    assert sweep.reorder_windows("alpha", rev) is True
    after = _tmux("list-windows", "-t", "alpha", "-F", "#{window_id}").stdout.split()
    assert after == rev


def test_reorder_windows_stale_is_noop(tmux_server):
    ids = _tmux("list-windows", "-t", "alpha", "-F", "#{window_id}").stdout.split()
    # Wrong count -> refuse rather than half-apply.
    assert sweep.reorder_windows("alpha", ids + ["@999"]) is False


def test_rename_window(tmux_server):
    win = _tmux("list-windows", "-t", "alpha", "-F", "#{window_id}").stdout.split()[0]
    assert sweep.rename_window("alpha", win, "editor") is True
    names = _tmux("list-windows", "-t", "alpha", "-F", "#{window_name}").stdout.split()
    assert "editor" in names


def test_switch_client_and_current_session(tmux_server):
    """Attach a real client on a pty, read its current session, switch it
    per-client, and confirm only that client moved."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("tmux", ["tmux", "-L", _SOCK, "attach", "-t", "alpha"])
        os._exit(1)
    try:
        time.sleep(1.0)
        tty = os.readlink(f"/proc/{pid}/fd/0")
        assert sweep.client_session(tty) == "alpha"
        assert sweep.switch_client(tty, "beta") is True
        time.sleep(0.3)
        assert sweep.client_session(tty) == "beta"
    finally:
        os.close(fd)
