"""Restart lifecycle: reading live state, stopping the exact process, relaunch.

``server_control`` reads the unified ``server-state.json`` (PID + port) the
server publishes, validates the PID with ``ps``, stops that exact process, and
relaunches on the same port. It also honors the legacy ``server-pid`` file so a
`merlin restart` can stop a server started by the previous release.

Safety: these tests spawn real processes and send real signals. ``MERLIN_HOME``
is already redirected to a per-test temp dir by the suite conftest, and the
pattern-kill fallback is replaced with a spy — its real patterns
(``uv run cli.py`` ...) match the developer's own running Merlin, so it must
never execute for real in the suite.
"""

import subprocess
import sys
import time
from unittest import mock

import pytest

import paths
import server_control


def _wait(pred, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _seed_state(pid: int, port: int = 3123) -> None:
    paths.write_server_state(pid=pid, port=port)


def _seed_legacy_pid(pid: int) -> None:
    p = paths.server_pid_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(pid))


@pytest.fixture
def env(monkeypatch):
    """conftest already isolates MERLIN_HOME; ensure data/ exists, self-managed
    mode, and a spied fallback so real pattern-kill never fires."""
    (paths.data_dir() / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("MERLIN_SUPERVISED", raising=False)
    monkeypatch.setattr(server_control, "_fallback_pattern_kill", mock.Mock())


@pytest.fixture
def spawn(tmp_path):
    """Spawn real, killable processes whose argv contains main.py."""
    procs: list[subprocess.Popen] = []

    def _spawn(argv_basename: str = "main.py") -> subprocess.Popen:
        d = tmp_path / f"proc{len(procs)}"
        d.mkdir()
        script = d / argv_basename
        script.write_text("import time\ntime.sleep(300)\n")
        p = subprocess.Popen([sys.executable, str(script)])
        procs.append(p)
        assert _wait(
            lambda: argv_basename in (server_control._ps_field(p.pid, "args=") or "")
        ), "process did not become visible to ps"
        return p

    yield _spawn
    for p in procs:
        p.kill()
        p.wait()


# ---------------------------------------------------------------------------
# pid_is_merlin
# ---------------------------------------------------------------------------


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
# live_state
# ---------------------------------------------------------------------------


class TestLiveState:
    def test_reads_state_pid_and_port(self, env, spawn):
        p = spawn("main.py")
        _seed_state(p.pid, port=8080)
        assert server_control.live_state() == (p.pid, 8080)

    def test_falls_back_to_legacy_pid_without_port(self, env, spawn):
        p = spawn("main.py")
        _seed_legacy_pid(p.pid)  # no state file
        assert server_control.live_state() == (p.pid, None)

    def test_none_when_no_files(self, env):
        assert server_control.live_state() == (None, None)

    def test_none_for_dead_pid_in_state(self, env):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        _seed_state(dead.pid, port=8080)
        assert server_control.live_state() == (None, None)

    def test_none_for_foreign_pid_in_state(self, env):
        foreign = subprocess.Popen(["sleep", "300"])
        try:
            _seed_state(foreign.pid, port=8080)
            assert server_control.live_state() == (None, None)
        finally:
            foreign.kill()
            foreign.wait()


# ---------------------------------------------------------------------------
# stop_server
# ---------------------------------------------------------------------------


class TestStopServer:
    def test_stops_and_returns_port(self, env, spawn):
        target = spawn("main.py")
        _seed_state(target.pid, port=8080)

        stopped, port = server_control.stop_server(timeout=2.0)

        assert stopped is True
        assert port == 8080
        assert _wait(lambda: target.poll() is not None), "target should be stopped"
        assert not paths.server_state_path().exists()
        server_control._fallback_pattern_kill.assert_not_called()

    def test_unrelated_process_survives(self, env, spawn):
        """The original bug: a decoy main.py must not be killed."""
        target = spawn("main.py")
        decoy = spawn("main.py")
        _seed_state(target.pid, port=3123)

        server_control.stop_server(timeout=2.0)

        assert _wait(lambda: target.poll() is not None)
        time.sleep(0.3)
        assert decoy.poll() is None, "decoy must survive — this is the bug"
        server_control._fallback_pattern_kill.assert_not_called()

    def test_legacy_pid_is_stopped(self, env, spawn):
        target = spawn("main.py")
        _seed_legacy_pid(target.pid)

        stopped, port = server_control.stop_server(timeout=2.0)

        assert stopped is True
        assert port is None  # legacy file carries no port
        assert _wait(lambda: target.poll() is not None)
        assert not paths.server_pid_path().exists()
        server_control._fallback_pattern_kill.assert_not_called()

    def test_stale_pid_stops_nothing_and_falls_back(self, env, spawn):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        decoy = spawn("main.py")
        _seed_state(dead.pid, port=3123)

        stopped, port = server_control.stop_server(timeout=2.0)

        assert stopped is False
        assert port is None
        time.sleep(0.3)
        assert decoy.poll() is None, "no innocent process should be signalled"
        assert not paths.server_state_path().exists()
        server_control._fallback_pattern_kill.assert_called_once()

    def test_foreign_pid_is_not_signalled(self, env):
        foreign = subprocess.Popen(["sleep", "300"])
        try:
            _seed_state(foreign.pid, port=3123)
            stopped, _ = server_control.stop_server(timeout=2.0)
            assert stopped is False
            time.sleep(0.3)
            assert foreign.poll() is None, "a non-Merlin PID must not be signalled"
            server_control._fallback_pattern_kill.assert_called_once()
        finally:
            foreign.kill()
            foreign.wait()

    def test_no_state_falls_back(self, env):
        stopped, _ = server_control.stop_server(timeout=2.0)
        assert stopped is False
        server_control._fallback_pattern_kill.assert_called_once()


# ---------------------------------------------------------------------------
# relaunch environment sanitization
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
        assert env["MERLIN_KEEP"] == "yes"


# ---------------------------------------------------------------------------
# restart: supervised vs self-managed, and port continuity
# ---------------------------------------------------------------------------


class TestRestart:
    def test_supervised_restart_does_not_relaunch(self, monkeypatch):
        monkeypatch.setenv("MERLIN_SUPERVISED", "1")
        relaunch = mock.Mock()
        monkeypatch.setattr(server_control, "_relaunch", relaunch)
        monkeypatch.setattr(
            server_control, "stop_server", mock.Mock(return_value=(True, 8080))
        )
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        assert server_control.restart() == 0
        relaunch.assert_not_called()

    def test_self_managed_restart_relaunches_on_same_port(self, monkeypatch):
        monkeypatch.delenv("MERLIN_SUPERVISED", raising=False)
        relaunch = mock.Mock()
        monkeypatch.setattr(server_control, "_relaunch", relaunch)
        monkeypatch.setattr(
            server_control, "stop_server", mock.Mock(return_value=(True, 8080))
        )
        monkeypatch.setattr(server_control, "_server_running", lambda: True)
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        assert server_control.restart() == 0
        relaunch.assert_called_once_with(8080)
