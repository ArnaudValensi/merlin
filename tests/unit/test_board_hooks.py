"""Tests for the Sessions board tmux hook scripts (terminal/hooks/).

  - agent-session-init.sh  — SessionStart companion: mints @agent_sid once and
    pins @agent_cwd once on the current pane's window.
  - agent-relate.sh        — stamps @agent_parent (the parent's @agent_sid) and
    @agent_relation on a freshly spawned window.

Driven against a throwaway tmux server on a private socket, reached via $TMUX
(the scripts call bare `tmux`), exactly as tmux sets it inside a run-shell hook.
Skips cleanly when tmux is not installed.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "terminal" / "hooks"
INIT_SH = HOOKS / "agent-session-init.sh"
RELATE_SH = HOOKS / "agent-relate.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


class Server:
    def __init__(self, socket: Path, home: Path):
        self.socket = str(socket)
        self.home = str(home)
        self._pid = None

    def _base(self):
        return ["tmux", "-S", self.socket, "-f", "/dev/null"]

    def _env(self):
        env = {"HOME": self.home, "TERM": "xterm-256color"}
        for var in ("LANG", "LC_ALL", "LC_CTYPE", "PATH"):
            val = os.environ.get(var)
            if val:
                env[var] = val
        env.setdefault("PATH", "/usr/bin:/bin")
        return env

    def tmux(self, *args) -> str:
        r = subprocess.run(
            [*self._base(), *args],
            capture_output=True,
            text=True,
            env=self._env(),
            check=False,
        )
        return r.stdout.rstrip("\n")

    def kill(self):
        subprocess.run(
            [*self._base(), "kill-server"],
            capture_output=True,
            env=self._env(),
            check=False,
        )

    @property
    def pid(self) -> str:
        if self._pid is None:
            self._pid = self.tmux("display-message", "-p", "#{pid}")
        return self._pid

    def _tmux_var(self) -> str:
        return f"{self.socket},{self.pid},0"

    def wopt(self, win: str, name: str) -> str:
        return self.tmux("show-option", "-wv", "-t", win, name)

    def set_wopt(self, win: str, name: str, value: str):
        self.tmux("set-option", "-w", "-t", win, name, value)

    def pane_of(self, win: str) -> str:
        return self.tmux("display-message", "-p", "-t", win, "#{pane_id}")

    def run(self, script: Path, *args, pane=None, cwd=None):
        env = self._env()
        env["TMUX"] = self._tmux_var()
        if pane is not None:
            env["TMUX_PANE"] = pane
        if cwd is not None:
            env["PWD"] = str(cwd)
        return subprocess.run(
            ["bash", str(script), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
        )


@pytest.fixture
def srv(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    s = Server(tmp_path / "s.sock", home)
    s.kill()
    s.tmux("new-session", "-d", "-s", "t", "-x", "100", "-y", "30")
    wins = {"a": s.tmux("display-message", "-p", "#{window_id}")}
    wins["b"] = s.tmux("new-window", "-P", "-F", "#{window_id}", "-t", "t")
    yield s, wins
    s.kill()


# ---------------------------------------------------------------------------
# agent-session-init.sh
# ---------------------------------------------------------------------------
class TestSessionInit:
    def test_mints_sid_when_absent(self, srv):
        s, w = srv
        assert s.wopt(w["a"], "@agent_sid") == ""
        r = s.run(INIT_SH, pane=s.pane_of(w["a"]), cwd=s.home)
        assert r.returncode == 0
        assert s.wopt(w["a"], "@agent_sid") != ""

    def test_keeps_existing_sid_across_resume(self, srv):
        s, w = srv
        s.set_wopt(w["a"], "@agent_sid", "fixed-id")
        s.run(INIT_SH, pane=s.pane_of(w["a"]), cwd=s.home)
        assert s.wopt(w["a"], "@agent_sid") == "fixed-id"

    def test_pins_cwd_to_launch_dir(self, srv, tmp_path):
        s, w = srv
        launch = tmp_path / "launchdir"
        launch.mkdir()
        s.run(INIT_SH, pane=s.pane_of(w["a"]), cwd=launch)
        assert s.wopt(w["a"], "@agent_cwd") == str(launch)

    def test_does_not_move_pinned_cwd(self, srv, tmp_path):
        s, w = srv
        s.set_wopt(w["a"], "@agent_cwd", "/original/dir")
        s.run(INIT_SH, pane=s.pane_of(w["a"]), cwd=tmp_path)
        assert s.wopt(w["a"], "@agent_cwd") == "/original/dir"

    def test_targets_the_panes_window(self, srv):
        s, w = srv
        s.run(INIT_SH, pane=s.pane_of(w["b"]), cwd=s.home)
        assert s.wopt(w["b"], "@agent_sid") != ""
        assert s.wopt(w["a"], "@agent_sid") == ""

    def test_noop_outside_tmux(self, tmp_path):
        env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
        r = subprocess.run(
            ["bash", str(INIT_SH)],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        assert r.returncode == 0  # clean no-op


# ---------------------------------------------------------------------------
# agent-relate.sh
# ---------------------------------------------------------------------------
class TestRelate:
    def test_stamps_parent_sid_and_relation(self, srv):
        s, w = srv
        s.set_wopt(w["a"], "@agent_sid", "parent-sid")  # a is the parent
        s.run(RELATE_SH, w["b"], "child", pane=s.pane_of(w["a"]))
        assert s.wopt(w["b"], "@agent_parent") == "parent-sid"
        assert s.wopt(w["b"], "@agent_relation") == "child"

    def test_sibling_relation(self, srv):
        s, w = srv
        s.set_wopt(w["a"], "@agent_sid", "p")
        s.run(RELATE_SH, w["b"], "sibling", pane=s.pane_of(w["a"]))
        assert s.wopt(w["b"], "@agent_relation") == "sibling"

    def test_explicit_parent_window_arg(self, srv):
        s, w = srv
        s.set_wopt(w["a"], "@agent_sid", "explicit-parent")
        # run from b's pane but name a as the parent explicitly
        s.run(RELATE_SH, w["b"], "child", w["a"], pane=s.pane_of(w["b"]))
        assert s.wopt(w["b"], "@agent_parent") == "explicit-parent"

    def test_bogus_relation_is_noop(self, srv):
        s, w = srv
        s.set_wopt(w["a"], "@agent_sid", "p")
        s.run(RELATE_SH, w["b"], "cousin", pane=s.pane_of(w["a"]))
        assert s.wopt(w["b"], "@agent_relation") == ""
        assert s.wopt(w["b"], "@agent_parent") == ""

    def test_relation_set_even_without_parent_sid(self, srv):
        # Parent window has no @agent_sid yet: relation still records, parent empty.
        s, w = srv
        s.run(RELATE_SH, w["b"], "sibling", pane=s.pane_of(w["a"]))
        assert s.wopt(w["b"], "@agent_relation") == "sibling"
        assert s.wopt(w["b"], "@agent_parent") == ""


# ---------------------------------------------------------------------------
# sweep.py against a real tmux server (parse + focus + kill)
# ---------------------------------------------------------------------------
class TestSweepIntegration:
    def test_sweep_parses_real_tmux(self, srv, monkeypatch):
        s, w = srv
        s.set_wopt(w["a"], "@agent_sid", "sid-a")
        s.set_wopt(w["a"], "@agent_state", "busy")
        s.set_wopt(
            w["a"], "@agent_cwd", "/tmp/proj one"
        )  # space survives (tab-delimited)
        monkeypatch.setenv("TMUX", s._tmux_var())
        from board import sweep

        rec = next((x for x in sweep.run_sweep() if x.sid == "sid-a"), None)
        assert rec is not None
        assert rec.state == "busy"
        assert rec.cwd == "/tmp/proj one"
        assert rec.is_agent is True

    def test_focus_and_kill_window(self, srv, monkeypatch):
        s, w = srv
        s.set_wopt(w["a"], "@agent_sid", "sid-a")
        s.set_wopt(w["a"], "@agent_state", "done")
        monkeypatch.setenv("TMUX", s._tmux_var())
        from board import sweep

        rec = next((x for x in sweep.run_sweep() if x.sid == "sid-a"), None)
        assert rec is not None
        assert sweep.focus_window(rec.session, rec.window_id) is True
        assert sweep.kill_window(rec.session, rec.window_id) is True
        assert all(x.window_id != rec.window_id for x in sweep.run_sweep())

    def test_kill_window_rejects_empty(self):
        from board import sweep

        assert sweep.kill_window("", "") is False
