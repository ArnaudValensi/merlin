"""The test suite can never reach the user's real tmux server.

Every tmux here is a fake on PATH that records its argv: no real server, no
real socket is ever touched, which is the point.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import tmux_isolation

FAKE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_TMUX_LOG"
echo forwarded
"""


@pytest.fixture
def fake_tmux(tmp_path, monkeypatch):
    """A recording ``tmux`` first on PATH; returns the path of its argv log."""
    bindir = tmp_path / "realbin"
    bindir.mkdir()
    fake = bindir / "tmux"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_TMUX_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def _argv(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


class TestKillServer:
    def test_kills_by_socket_never_by_environment(self, fake_tmux, tmp_path):
        sock = tmux_isolation.socket_path(tmp_path)
        sock.parent.mkdir()
        sock.write_text("")  # stands in for the socket
        assert tmux_isolation.kill_server(tmp_path) is True
        assert _argv(fake_tmux) == ["-S", str(sock), "kill-server"]

    def test_a_missing_socket_runs_nothing(self, fake_tmux, tmp_path):
        """The case that killed a real server: the directory is gone, and a
        bare kill-server would fall back to /tmp/tmux-<uid>/default."""
        assert tmux_isolation.kill_server(tmp_path / "gone") is False
        assert _argv(fake_tmux) == []

    def test_tmux_variable_is_not_passed_on(self, fake_tmux, tmp_path, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
        seen = {}

        def run(cmd, env=None, **kw):
            seen["env"] = env
            return subprocess.CompletedProcess(cmd, 0)

        sock = tmux_isolation.socket_path(tmp_path)
        sock.parent.mkdir()
        sock.write_text("")
        tmux_isolation.kill_server(tmp_path, run=run)
        assert "TMUX" not in seen["env"]


class TestGuardShim:
    """The shim installed first on PATH for the whole pytest session."""

    @pytest.fixture
    def shim(self, fake_tmux, tmp_path):
        real = shutil.which("tmux")  # the fake, first on PATH
        assert real and real.startswith(str(tmp_path))
        return tmux_isolation.write_shim(tmp_path / "shim", real)

    def _run(self, shim: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
        base = {
            "PATH": os.environ["PATH"],
            "FAKE_TMUX_LOG": os.environ["FAKE_TMUX_LOG"],
        }
        return subprocess.run(
            [str(shim), *args],
            capture_output=True,
            text=True,
            env={**base, **env},
            check=False,
        )

    def test_refuses_when_the_socket_directory_is_gone(self, shim, fake_tmux, tmp_path):
        r = self._run(
            shim, "kill-server", env={"TMUX_TMPDIR": str(tmp_path / "vanished")}
        )
        assert r.returncode == 1
        assert "refused" in r.stderr
        assert _argv(fake_tmux) == []

    def test_refuses_without_any_socket_directory(self, shim, fake_tmux):
        r = self._run(shim, "list-sessions", env={})
        assert r.returncode == 1
        assert _argv(fake_tmux) == []

    def test_forwards_with_a_live_socket_directory(self, shim, fake_tmux, tmp_path):
        r = self._run(shim, "list-sessions", env={"TMUX_TMPDIR": str(tmp_path)})
        assert r.returncode == 0 and "forwarded" in r.stdout
        assert _argv(fake_tmux) == ["list-sessions"]

    def test_forwards_an_explicit_socket(self, shim, fake_tmux, tmp_path):
        r = self._run(shim, "-S", "/x/sock", "kill-server", env={})
        assert r.returncode == 0
        assert _argv(fake_tmux) == ["-S", "/x/sock", "kill-server"]

    def test_forwards_inside_a_test_server(self, shim, fake_tmux, tmp_path):
        sock = tmp_path / "private.sock"
        import socket as socket_mod

        with socket_mod.socket(socket_mod.AF_UNIX) as s:
            s.bind(str(sock))
            r = self._run(shim, "display-message", env={"TMUX": f"{sock},1,0"})
        assert r.returncode == 0
        assert _argv(fake_tmux) == ["display-message"]

    def test_refuses_the_real_servers_tmux_variable(self, shim, fake_tmux):
        real = tmux_isolation.real_socket_dir() / "default"
        r = self._run(shim, "kill-server", env={"TMUX": f"{real},1,0"})
        assert r.returncode == 1
        assert _argv(fake_tmux) == []


def test_the_session_guard_is_live():
    """What this very pytest session runs: tmux is the shim, TMUX is unset
    and TMUX_TMPDIR is the session's private directory, which exists."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    found = Path(shutil.which("tmux") or "")
    assert found.name == "tmux" and found.parent.name == "bin"
    assert "Merlin test guard" in found.read_text()
    assert "TMUX" not in os.environ
    assert Path(os.environ["TMUX_TMPDIR"]).is_dir()
    assert found.parent.parent == Path(os.environ["TMUX_TMPDIR"])


def test_stale_directories_of_a_live_run_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(tmux_isolation.tempfile, "gettempdir", lambda: str(tmp_path))
    young = tmp_path / "merlin-tmux-young"
    young.mkdir()
    old = tmp_path / "merlin-tmux-old"
    old.mkdir()
    ancient = 10**9
    os.utime(old, (ancient, ancient))
    mine = tmp_path / "merlin-tmux-mine"
    mine.mkdir()
    os.utime(mine, (ancient, ancient))
    removed = tmux_isolation.remove_stale("merlin-tmux-", mine)
    assert removed == [old]
    assert young.exists() and mine.exists() and not old.exists()
