"""Tests for the agent-state tmux hook scripts (terminal/hooks/).

  - agent-state.sh idle|busy|done  — sets @agent_state on the CURRENT PANE's
    window (driven by the Claude Code hooks).
  - agent-state-switch.sh          — clears the green 'done' pill on window
    change (arrive-clears, leave-clears), touching only state 'done'.

Both are driven against a throwaway tmux server on a private socket. The
scripts call bare `tmux`, so they are pointed at that server via the $TMUX
environment variable (socket_path,pid,session), exactly as tmux itself sets it
inside a run-shell hook. Skips cleanly when tmux is not installed.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "terminal" / "hooks"
STATE_SH = HOOKS / "agent-state.sh"
SWITCH_SH = HOOKS / "agent-state-switch.sh"
TMUX_CONF = REPO_ROOT / "terminal" / "tmux.conf"

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


class Server:
    """Throwaway tmux server; scripts reach it via $TMUX (bare `tmux`)."""

    def __init__(self, socket: Path, home: Path):
        self.socket = str(socket)
        self.home = str(home)
        self._pid = None

    def _base(self):
        return ["tmux", "-S", self.socket, "-f", "/dev/null"]

    def _plain_env(self):
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
            env=self._plain_env(),
            check=False,
        )
        return r.stdout.rstrip("\n")

    def kill(self):
        subprocess.run(
            [*self._base(), "kill-server"],
            capture_output=True,
            env=self._plain_env(),
            check=False,
        )

    @property
    def pid(self) -> str:
        if self._pid is None:
            self._pid = self.tmux("display-message", "-p", "#{pid}")
        return self._pid

    def _tmux_var(self) -> str:
        # Format tmux itself uses: socket_path,server_pid,session_id.
        return f"{self.socket},{self.pid},0"

    def hook_env(self, pane: str | None = None) -> dict:
        env = self._plain_env()
        env["TMUX"] = self._tmux_var()
        if pane is not None:
            env["TMUX_PANE"] = pane
        return env

    def run_state(self, *args, pane=None, stdin=None):
        """Invoke agent-state.sh with TMUX (+ optional TMUX_PANE).

        The script drains stdin (`cat`) so a JSON-piping caller never gets
        EPIPE. When no stdin is supplied we hand it an already-closed /dev/null
        so the drain returns immediately and the test can never hang on an
        inherited open stdin.
        """
        env = self._plain_env()
        env["TMUX"] = self._tmux_var()
        if pane is not None:
            env["TMUX_PANE"] = pane
        kwargs = {}
        if stdin is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = stdin
        return subprocess.run(
            ["bash", str(STATE_SH), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=False,
            **kwargs,
        )

    def run_switch(self):
        """Invoke agent-state-switch.sh; it reads the CURRENT window."""
        env = self._plain_env()
        env["TMUX"] = self._tmux_var()
        return subprocess.run(
            ["bash", str(SWITCH_SH)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=False,
        )

    # --- helpers ---------------------------------------------------------
    def state(self, win: str) -> str:
        return self.tmux("show-option", "-wv", "-t", win, "@agent_state")

    def prev_win(self) -> str:
        return self.tmux("show-option", "-sv", "@agent_state_prev_win")

    def set_state(self, win: str, value: str):
        self.tmux("set-option", "-w", "-t", win, "@agent_state", value)

    def select(self, win: str):
        self.tmux("select-window", "-t", win)

    def pane_of(self, win: str) -> str:
        return self.tmux("display-message", "-p", "-t", win, "#{pane_id}")


@pytest.fixture
def srv(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    s = Server(tmp_path / "s.sock", home)
    s.kill()
    s.tmux("new-session", "-d", "-s", "t", "-x", "100", "-y", "30")
    wins = {"a": s.tmux("display-message", "-p", "#{window_id}")}
    for name in ("b", "c"):
        wins[name] = s.tmux("new-window", "-P", "-F", "#{window_id}", "-t", "t")
    yield s, wins
    s.kill()


# ---------------------------------------------------------------------------
# agent-state.sh  (state setter)
# ---------------------------------------------------------------------------
class TestStateSetter:
    def test_sets_busy(self, srv):
        s, w = srv
        r = s.run_state("busy", pane=s.pane_of(w["a"]))
        assert r.returncode == 0
        assert s.state(w["a"]) == "busy"

    def test_sets_done(self, srv):
        s, w = srv
        s.run_state("done", pane=s.pane_of(w["a"]))
        assert s.state(w["a"]) == "done"

    def test_sets_idle(self, srv):
        s, w = srv
        s.run_state("idle", pane=s.pane_of(w["a"]))
        assert s.state(w["a"]) == "idle"

    def test_defaults_to_idle_without_arg(self, srv):
        s, w = srv
        s.run_state(pane=s.pane_of(w["a"]))
        assert s.state(w["a"]) == "idle"

    def test_targets_the_panes_window_not_the_active_one(self, srv):
        """A background window can go done while another is active."""
        s, w = srv
        s.select(w["a"])  # window a is active
        s.run_state("done", pane=s.pane_of(w["b"]))  # but pane is in b
        assert s.state(w["b"]) == "done"
        assert s.state(w["a"]) == ""  # active window untouched

    def test_noop_outside_tmux(self, srv):
        s, w = srv
        env = s._plain_env()  # no TMUX / TMUX_PANE
        r = subprocess.run(
            ["bash", str(STATE_SH), "busy"],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        assert r.returncode == 0  # clean no-op, never blocks a session

    def test_drains_stdin_without_hanging(self, srv):
        s, w = srv
        # Claude Code pipes JSON on stdin; the script must drain and not hang.
        r = s.run_state(
            "busy",
            pane=s.pane_of(w["a"]),
            stdin='{"session_id":"x","cwd":"/tmp"}',
        )
        assert r.returncode == 0
        assert s.state(w["a"]) == "busy"


# ---------------------------------------------------------------------------
# agent-state-switch.sh  (unread / done-pill clearing)
# ---------------------------------------------------------------------------
class TestSwitchClear:
    def test_arrive_at_done_clears_it(self, srv):
        s, w = srv
        s.set_state(w["b"], "done")
        s.select(w["b"])  # you visit the unread finished window
        s.run_switch()
        assert s.state(w["b"]) == "idle"

    def test_leave_clears_when_watching_it_finish(self, srv):
        s, w = srv
        s.select(w["a"])
        s.run_switch()  # establishes prev = a
        assert s.prev_win() == w["a"]
        s.set_state(w["a"], "done")  # a finishes while you watch it
        s.select(w["b"])
        s.run_switch()  # cur=b, prev=a -> leave-clears a
        assert s.state(w["a"]) == "idle"
        assert s.state(w["b"]) == ""  # b was never done

    def test_background_done_stays_green(self, srv):
        s, w = srv
        s.select(w["a"])
        s.run_switch()
        s.set_state(w["c"], "done")  # c finishes in the background
        # bounce between a and b, never visiting c
        s.select(w["b"])
        s.run_switch()
        s.select(w["a"])
        s.run_switch()
        assert s.state(w["c"]) == "done"  # still unread

    def test_busy_is_never_cleared(self, srv):
        s, w = srv
        s.set_state(w["b"], "busy")
        s.select(w["b"])
        s.run_switch()
        assert s.state(w["b"]) == "busy"

    def test_ask_survives_arriving_at_the_window(self, srv):
        """Load-bearing: 'done' means unread, so looking at it is enough to
        clear it. 'ask' means an unanswered dialog is still open, so looking at
        it must NOT clear it. Only answering (PostToolUse/PostToolBatch -> busy)
        does. The switch script gets this right by only ever touching 'done',
        but that is easy to break, hence this regression test."""
        s, w = srv
        s.set_state(w["b"], "ask")
        s.select(w["b"])
        s.run_switch()
        assert s.state(w["b"]) == "ask"

    def test_ask_survives_leaving_the_window(self, srv):
        s, w = srv
        s.set_state(w["b"], "ask")
        s.select(w["b"])
        s.run_switch()
        s.select(w["c"])
        s.run_switch()
        assert s.state(w["b"]) == "ask"

    def test_idle_is_left_alone(self, srv):
        s, w = srv
        s.set_state(w["b"], "idle")
        s.select(w["b"])
        s.run_switch()
        assert s.state(w["b"]) == "idle"

    def test_unset_window_is_left_unset(self, srv):
        s, w = srv
        s.select(w["b"])  # b has no @agent_state
        r = s.run_switch()
        assert r.returncode == 0
        assert s.state(w["b"]) == ""

    def test_tracks_previous_window(self, srv):
        s, w = srv
        s.select(w["c"])
        s.run_switch()
        assert s.prev_win() == w["c"]

    def test_noop_outside_tmux(self, srv):
        s, w = srv
        env = s._plain_env()  # no TMUX
        r = subprocess.run(
            ["bash", str(SWITCH_SH)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=False,
        )
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# Integration: the REAL terminal/tmux.conf set-hook fires the switch-clear
# script on a window change, located via $MERLIN_TERMINAL_HOOKS (the var
# terminal/routes.py exports). No manual invocation of the script here.
# ---------------------------------------------------------------------------
class TestSetHookIntegration:
    @staticmethod
    def _conf_env(home: Path) -> dict:
        env = {
            "HOME": str(home),
            "TERM": "xterm-256color",
            "MERLIN_TERMINAL_HOOKS": str(HOOKS),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        for var in ("LANG", "LC_ALL", "LC_CTYPE"):
            if os.environ.get(var):
                env[var] = os.environ[var]
        return env

    def _tmux(self, socket: Path, home: Path, *args) -> str:
        r = subprocess.run(
            ["tmux", "-S", str(socket), "-f", str(TMUX_CONF), *args],
            capture_output=True,
            text=True,
            env=self._conf_env(home),
            check=False,
        )
        return r.stdout.rstrip("\n")

    def test_visiting_done_window_clears_via_conf_set_hook(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        sock = tmp_path / "s.sock"
        t = lambda *a: self._tmux(sock, home, *a)  # noqa: E731
        t("kill-server")
        t("new-session", "-d", "-s", "t", "-x", "100", "-y", "30")
        t("display-message", "-p", "#{window_id}")  # window a (unused id)
        b = t("new-window", "-P", "-F", "#{window_id}", "-t", "t")
        t("set-option", "-w", "-t", b, "@agent_state", "done")  # b finished
        # Visit b — the conf's session-window-changed set-hook must run
        # agent-state-switch.sh (via $MERLIN_TERMINAL_HOOKS) and clear it.
        t("select-window", "-t", b)
        t("run-shell", "true")  # drain the command queue
        state = "done"
        for _ in range(20):
            state = t("show-option", "-wv", "-t", b, "@agent_state")
            if state != "done":
                break
            time.sleep(0.05)
        prev = t("show-option", "-sv", "@agent_state_prev_win")
        t("kill-server")
        assert state == "idle", f"set-hook did not clear the done pill: {state!r}"
        assert prev == b  # switch-clear tracked the previous window


class TestWindowFallback:
    """agent-window.sh: a hook whose environment lost TMUX_PANE and TMUX (Claude
    Code 2.1.273 runs hooks through a helper daemon with a trimmed
    environment) still finds its window, by walking up its ancestors to the
    pane's shell. Only inside a Merlin terminal (MERLIN_TERMINAL_HOOKS set)."""

    @pytest.fixture
    def default_server(self, tmp_path):
        """A throwaway server on the DEFAULT socket of a private TMUX_TMPDIR,
        so a bare `tmux` with no $TMUX at all reaches it."""
        # Unix socket paths cap at about 108 characters: a short dir in /tmp,
        # not pytest's long tmp_path.
        import tempfile

        tmpdir = Path(tempfile.mkdtemp(prefix="mhk-"))
        sock_dir = tmpdir / f"tmux-{os.getuid()}"
        sock_dir.mkdir(mode=0o700)
        s = Server(sock_dir / "default", tmp_path / "home")
        (tmp_path / "home").mkdir()
        s.tmpdir = str(tmpdir)
        s.tmux("new-session", "-d", "-s", "t", "-n", "a", "sleep 300")
        s.tmux("new-window", "-d", "-t", "t", "-n", "b", "sleep 300")
        # A shell in window c to run the hook from, as a pane descendant. Bash
        # without init files: a fresh home's zsh wizard would eat keystrokes.
        s.tmux("new-window", "-d", "-t", "t", "-n", "c", "bash --norc --noprofile")
        for _ in range(50):
            if s.tmux("display-message", "-p", "-t", "t:c", "#{pane_current_command}"):
                break
            time.sleep(0.1)
        yield s
        s.kill()

    def _run_in_pane(self, s, window, command):
        # Typed into the pane's shell: the hook is then a descendant of the
        # pane process, which is what the fallback walks up to.
        s.tmux("send-keys", "-t", f"t:{window}", command, "Enter")

    def _wait_state(self, s, window, expected, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if (
                s.tmux("show-option", "-wv", "-t", f"t:{window}", "@agent_state")
                == expected
            ):
                return True
            time.sleep(0.1)
        return False

    def test_stamps_the_panes_window_without_tmux_variables(self, default_server):
        s = default_server
        cmd = (
            f"env -u TMUX -u TMUX_PANE TMUX_TMPDIR={s.tmpdir} MERLIN_TERMINAL_HOOKS=1 "
            f"bash {STATE_SH} busy < /dev/null"
        )
        self._run_in_pane(s, "c", cmd)
        assert self._wait_state(s, "c", "busy"), s.tmux("list-windows")
        assert s.tmux("show-option", "-wv", "-t", "t:a", "@agent_state") == ""

    def test_stays_a_noop_outside_a_merlin_terminal(self, default_server):
        s = default_server
        cmd = (
            f"env -u TMUX -u TMUX_PANE -u MERLIN_TERMINAL_HOOKS TMUX_TMPDIR={s.tmpdir} "
            f"bash {STATE_SH} busy < /dev/null; tmux set-option -w @probe ran"
        )
        self._run_in_pane(s, "c", cmd)
        deadline = time.time() + 8
        while time.time() < deadline:
            if s.tmux("show-option", "-wv", "-t", "t:c", "@probe") == "ran":
                break
            time.sleep(0.1)
        assert s.tmux("show-option", "-wv", "-t", "t:c", "@agent_state") == ""


# ---------------------------------------------------------------------------
# Integration: the REAL terminal/tmux.conf Esc binding. An interrupt (Esc
# mid-turn) fires no Claude Code hook, so the conf marks a busy/ask window idle
# from the keypress itself. Key bindings only run for a real client, so the
# server is driven through a client attached on a pty, not send-keys.
# ---------------------------------------------------------------------------
class TestEscapeBinding:
    @pytest.fixture
    def attached(self, tmp_path):
        import pty
        import select

        home = tmp_path / "home"
        home.mkdir()
        sock = tmp_path / "s.sock"
        env = TestSetHookIntegration._conf_env(home)

        def t(*args):
            r = subprocess.run(
                ["tmux", "-S", str(sock), "-f", str(TMUX_CONF), *args],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            return r.stdout.rstrip("\n")

        t("kill-server")
        # `cat -v` echoes every byte it receives: an Esc shows up as ^[.
        t("new-session", "-d", "-s", "t", "-x", "80", "-y", "24", "cat -v")
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - child
            os.execvpe("tmux", ["tmux", "-S", str(sock), "attach", "-t", "t"], env)

        def drain(seconds):
            end = time.time() + seconds
            while time.time() < end:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    try:
                        os.read(fd, 65536)
                    except OSError:
                        return

        drain(1.0)

        def press(raw: bytes):
            os.write(fd, raw)
            drain(0.5)

        def pane_bytes() -> str:
            return "".join(t("capture-pane", "-p", "-t", "t").split())

        def state() -> str:
            return t("show-option", "-wv", "-t", "t", "@agent_state")

        yield t, press, pane_bytes, state
        t("kill-server")
        os.close(fd)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    @pytest.mark.parametrize("initial", ["busy", "ask"])
    def test_esc_marks_a_working_or_asking_window_idle(self, attached, initial):
        t, press, pane_bytes, state = attached
        t("set-option", "-w", "-t", "t", "@agent_state", initial)
        press(b"\x1b")
        assert state() == "idle"
        assert pane_bytes().endswith("^["), "Esc must still reach the pane"

    @pytest.mark.parametrize("initial", ["done", "idle"])
    def test_esc_leaves_other_states_alone(self, attached, initial):
        t, press, pane_bytes, state = attached
        t("set-option", "-w", "-t", "t", "@agent_state", initial)
        press(b"\x1b")
        assert state() == initial
        assert pane_bytes().endswith("^[")

    def test_esc_in_a_plain_window_stays_unset(self, attached):
        t, press, pane_bytes, state = attached
        press(b"\x1b")
        assert state() == ""
        assert pane_bytes().endswith("^[")

    def test_meta_chord_is_not_an_interrupt(self, attached):
        # Esc followed at once by a key is M-x to tmux: a different key, which
        # the pane receives intact and which never touches the pill.
        t, press, pane_bytes, state = attached
        t("set-option", "-w", "-t", "t", "@agent_state", "busy")
        press(b"\x1bx")
        assert state() == "busy"
        assert pane_bytes().endswith("^[x")
