"""Restart lifecycle: PID-file cleanup and server_control's PID-based stop.

Two layers:
  1. ``main._remove_pid_file`` — clears the file only while it still names this
     process.
  2. ``server_control`` — reads the recorded PID, validates it with ``ps``, and
     stops that exact process, leaving unrelated ones alone.

Safety: these tests spawn real processes and send real signals. Everything is
isolated onto a throwaway ``MERLIN_HOME``, and the pattern-kill fallback is
replaced with a spy — its real patterns (``uv run cli.py`` ...) match the
developer's own running Merlin, so it must never execute for real in the suite.
"""

import subprocess
import sys
import time
from unittest import mock

import pytest

import paths
import server_control
from main import _remove_pid_file


# ---------------------------------------------------------------------------
# _remove_pid_file
# ---------------------------------------------------------------------------


class TestRemovePidFile:
    def test_removes_when_owner_matches(self, tmp_path):
        pid_file = tmp_path / "server-pid"
        pid_file.write_text("4242")
        _remove_pid_file(pid_file, 4242)
        assert not pid_file.exists()

    def test_keeps_when_a_successor_owns_it(self, tmp_path):
        """A slow shutdown must not delete a newcomer's file."""
        pid_file = tmp_path / "server-pid"
        pid_file.write_text("9999")
        _remove_pid_file(pid_file, 4242)
        assert pid_file.read_text() == "9999"

    def test_tolerates_trailing_whitespace(self, tmp_path):
        pid_file = tmp_path / "server-pid"
        pid_file.write_text("4242\n")
        _remove_pid_file(pid_file, 4242)
        assert not pid_file.exists()

    def test_silent_when_missing(self, tmp_path):
        _remove_pid_file(tmp_path / "nope", 4242)  # no raise

    def test_silent_on_garbage(self, tmp_path):
        pid_file = tmp_path / "server-pid"
        pid_file.write_text("not-a-pid")
        _remove_pid_file(pid_file, 4242)
        assert pid_file.exists()


# ---------------------------------------------------------------------------
# Fixtures + helpers for the real-process tests
# ---------------------------------------------------------------------------


def _wait(pred, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """Point MERLIN_HOME at a sandbox so the PID path can never read the live
    server's file, and neuter the fallback so its live patterns never fire."""
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    monkeypatch.setenv("MERLIN_HOME", str(home))
    monkeypatch.delenv("MERLIN_SUPERVISED", raising=False)
    monkeypatch.setattr(server_control, "_fallback_pattern_kill", mock.Mock())
    return home


@pytest.fixture
def spawn(tmp_path):
    """Spawn real, killable processes whose argv contains main.py, tracked for
    teardown."""
    procs: list[subprocess.Popen] = []

    def _spawn(argv_basename: str = "main.py") -> subprocess.Popen:
        d = tmp_path / f"proc{len(procs)}"
        d.mkdir()
        script = d / argv_basename
        script.write_text("import time\ntime.sleep(300)\n")
        p = subprocess.Popen([sys.executable, str(script)])
        procs.append(p)
        # Wait until the exec'd command line is visible to ps, not merely until
        # Popen returns — otherwise a validation check can race the fork/exec.
        assert _wait(
            lambda: argv_basename in (server_control._ps_field(p.pid, "args=") or "")
        ), "process did not become visible to ps"
        return p

    yield _spawn
    for p in procs:
        p.kill()
        p.wait()


def _write_pid(home, pid: int) -> None:
    (home / "data" / "server-pid").write_text(str(pid))


# ---------------------------------------------------------------------------
# read_server_pid / pid_is_merlin
# ---------------------------------------------------------------------------


class TestReadServerPid:
    def test_reads_written_pid(self, isolated_home):
        _write_pid(isolated_home, 4242)
        assert server_control.read_server_pid() == 4242

    def test_tolerates_whitespace(self, isolated_home):
        (isolated_home / "data" / "server-pid").write_text("  4242\n")
        assert server_control.read_server_pid() == 4242

    def test_none_when_missing(self, isolated_home):
        assert server_control.read_server_pid() is None

    def test_none_on_garbage(self, isolated_home):
        (isolated_home / "data" / "server-pid").write_text("junk")
        assert server_control.read_server_pid() is None


class TestPidIsMerlin:
    def test_true_for_our_merlin_like_process(self, spawn):
        p = spawn("main.py")
        assert server_control.pid_is_merlin(p.pid) is True

    def test_false_for_non_merlin_process(self):
        foreign = subprocess.Popen(["sleep", "300"])
        try:
            assert server_control.pid_is_merlin(foreign.pid) is False
        finally:
            foreign.kill()
            foreign.wait()

    def test_false_for_dead_pid(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        assert server_control.pid_is_merlin(dead.pid) is False


# ---------------------------------------------------------------------------
# stop_server
# ---------------------------------------------------------------------------


class TestStopServer:
    def test_stops_the_recorded_pid(self, isolated_home, spawn):
        target = spawn("main.py")
        _write_pid(isolated_home, target.pid)

        assert server_control.stop_server(timeout=2.0) is True
        assert _wait(lambda: target.poll() is not None), "target should be stopped"
        assert not paths.server_pid_path().exists()
        server_control._fallback_pattern_kill.assert_not_called()

    def test_unrelated_process_survives(self, isolated_home, spawn):
        """The original bug: a decoy main.py must not be killed."""
        target = spawn("main.py")
        decoy = spawn("main.py")
        _write_pid(isolated_home, target.pid)

        server_control.stop_server(timeout=2.0)

        assert _wait(lambda: target.poll() is not None)
        time.sleep(0.3)
        assert decoy.poll() is None, "decoy must survive — this is the bug"
        server_control._fallback_pattern_kill.assert_not_called()

    def test_stale_pid_stops_nothing_and_falls_back(self, isolated_home, spawn):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        decoy = spawn("main.py")
        _write_pid(isolated_home, dead.pid)

        assert server_control.stop_server(timeout=2.0) is False
        time.sleep(0.3)
        assert decoy.poll() is None, "no innocent process should be signalled"
        assert not paths.server_pid_path().exists()
        server_control._fallback_pattern_kill.assert_called_once()

    def test_foreign_pid_is_not_signalled(self, isolated_home):
        foreign = subprocess.Popen(["sleep", "300"])
        try:
            _write_pid(isolated_home, foreign.pid)
            assert server_control.stop_server(timeout=2.0) is False
            time.sleep(0.3)
            assert foreign.poll() is None, "a non-Merlin PID must not be signalled"
            server_control._fallback_pattern_kill.assert_called_once()
        finally:
            foreign.kill()
            foreign.wait()

    def test_no_pid_file_falls_back(self, isolated_home):
        assert server_control.stop_server(timeout=2.0) is False
        server_control._fallback_pattern_kill.assert_called_once()


# ---------------------------------------------------------------------------
# relaunch environment sanitization (moved out of restart.sh)
# ---------------------------------------------------------------------------


class TestRelaunchEnv:
    def test_strips_tmux_and_forces_term(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux/default,1,0")
        monkeypatch.setenv("TMUX_PANE", "%3")
        monkeypatch.setenv("TERM", "dumb")
        monkeypatch.setenv("MERLIN_KEEP", "yes")

        env = server_control._relaunch_env()

        assert env["TERM"] == "xterm-256color"
        assert "TMUX" not in env
        assert "TMUX_PANE" not in env
        assert env["MERLIN_KEEP"] == "yes"  # unrelated vars pass through


# ---------------------------------------------------------------------------
# supervised mode: restart stops and does not relaunch
# ---------------------------------------------------------------------------


class TestSupervisedRestart:
    def test_supervised_restart_does_not_relaunch(self, isolated_home, monkeypatch):
        monkeypatch.setenv("MERLIN_SUPERVISED", "1")
        relaunch = mock.Mock()
        monkeypatch.setattr(server_control, "_relaunch", relaunch)
        monkeypatch.setattr(server_control, "stop_server", mock.Mock(return_value=True))
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        assert server_control.restart() == 0
        relaunch.assert_not_called()

    def test_self_managed_restart_relaunches(self, isolated_home, monkeypatch):
        monkeypatch.delenv("MERLIN_SUPERVISED", raising=False)
        relaunch = mock.Mock()
        monkeypatch.setattr(server_control, "_relaunch", relaunch)
        monkeypatch.setattr(server_control, "stop_server", mock.Mock(return_value=True))
        monkeypatch.setattr(server_control, "_server_running", lambda: True)
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        assert server_control.restart() == 0
        relaunch.assert_called_once()
