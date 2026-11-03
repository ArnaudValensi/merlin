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
        wins[name] = s.tmux("new-window", "-P", "-F", "#{window_id}", "-t", "t:")
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

    def test_a_follow_up_after_a_job_finished_is_stepping_in(self, srv):
        """busy once the run has finished: someone gave the agent a follow-up.
        Before the finish, or any other state, it is only the run."""
        s, w = srv
        pane = s.pane_of(w["a"])
        stepped = lambda: s.tmux(  # noqa: E731
            "show-option", "-wqv", "-t", w["a"], "@job_stepped_in"
        ).strip()
        s.run_state("busy", pane=pane)
        assert stepped() == ""
        s.tmux("set-option", "-w", "-t", w["a"], "@job_finished", "1000")
        s.run_state("done", pane=pane)
        assert stepped() == ""
        s.run_state("busy", pane=pane)
        assert stepped() == "1"

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
        # Claude Code pipes JSON on stdin; the script must read it and not
        # hang. Run from outside the pane with a payload no window carries,
        # it proves no window and stamps none (agent-window.sh).
        r = s.run_state(
            "busy",
            pane=s.pane_of(w["a"]),
            stdin='{"session_id":"x","cwd":"/tmp"}',
        )
        assert r.returncode == 0
        assert s.state(w["a"]) == ""


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
        b = t("new-window", "-P", "-F", "#{window_id}", "-t", "t:")
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


class TestDaemonRunHooks:
    """agent-window.sh: Codex 0.165 runs hooks in its app-server daemon, with
    the daemon's environment: TMUX_PANE names whichever pane started the
    daemon (dead here), and no ancestor is a pane. The hook binds only on
    what is tied to the conversation: the window stamped with its session
    id, and at a job's first prompt, the one window carrying that job id in
    that cwd with no conversation. A SessionStart for a session no window
    carries binds nothing, whatever the panes in that directory look like."""

    INIT_SH = HOOKS / "agent-session-init.sh"

    @pytest.fixture
    def server(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        s = Server(tmp_path / "d.sock", home)
        s.kill()
        # A "codex" TUI: a script named codex, so ps reports comm=codex, in a
        # pane opened in the session's directory. A plain shell window in
        # the same directory must not be picked.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        fake = bindir / "codex"
        fake.write_text("#!/bin/sh\nsleep 300\n")
        fake.chmod(0o755)
        work = tmp_path / "proj"
        work.mkdir()
        other = tmp_path / "elsewhere"
        other.mkdir()
        s.tmux(
            "new-session", "-d", "-s", "t", "-n", "shell", "-c", str(work), "sleep 300"
        )
        s.tmux("new-window", "-d", "-t", "t", "-n", "agent", "-c", str(work), str(fake))
        s.tmux("new-window", "-d", "-t", "t", "-n", "far", "-c", str(other), str(fake))
        for _ in range(50):
            if (
                s.tmux(
                    "display-message", "-p", "-t", "t:agent", "#{pane_current_command}"
                )
                == "codex"
            ):
                break
            time.sleep(0.1)
        yield s, work
        s.kill()

    @staticmethod
    def _transcript(created: float) -> str:
        stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(created))
        return f"/home/u/.codex/sessions/rollout-{stamp}-abc.jsonl"

    def _start(
        self, conv: str, work, created: float | None = None, source="startup"
    ) -> str:
        created = time.time() if created is None else created
        return (
            f'{{"session_id":"{conv}","cwd":"{work}","hook_event_name":"SessionStart",'
            f'"transcript_path":"{self._transcript(created)}","source":"{source}"}}'
        )

    def _job_prompt(self, conv: str, work, job: str = "digest") -> str:
        return (
            f'{{"session_id":"{conv}","cwd":"{work}","hook_event_name":"UserPromptSubmit",'
            f'"prompt":"[Job: {job}]\\n\\nDo the thing"}}'
        )

    def _hook(self, s, script, args, pane, payload):
        env = s.hook_env(pane)
        return subprocess.run(
            ["bash", str(script), *args],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
            check=False,
        )

    def _opt(self, s, window, name):
        return s.tmux("show-option", "-wv", "-t", f"t:{window}", name)

    def _conv(self, s, window):
        return self._opt(s, window, "@agent_conv")

    def _state(self, s, window):
        return self._opt(s, window, "@agent_state")

    def _bind_job(self, s, work, window="agent", job="digest", conv="conv-j"):
        """A job window (its @job_id and @agent_cwd stamped by Merlin) whose
        agent sent the prompt Merlin wrote: bound by the job id."""
        s.tmux("set-option", "-w", "-t", f"t:{window}", "@job_id", job)
        s.tmux("set-option", "-w", "-t", f"t:{window}", "@agent_cwd", str(work))
        self._hook(s, STATE_SH, ["busy"], "%999", self._job_prompt(conv, work, job))

    def test_a_session_start_the_daemon_runs_binds_nothing(self, server):
        """The lone, just-started Codex TUI in that directory is not proof
        that it emitted the event: nothing is stamped."""
        s, work = server
        payload = self._start("conv-9", work)
        self._hook(s, self.INIT_SH, ["codex"], "%999", payload)
        self._hook(s, STATE_SH, ["idle"], "%999", payload)
        for window in ("agent", "shell", "far"):
            assert self._conv(s, window) == "" and self._state(s, window) == ""
            assert self._opt(s, window, "@agent_transcript") == ""

    def test_an_older_session_starting_anew_never_takes_a_newer_pane(
        self, server, tmp_path
    ):
        """R1: pane A's Codex has run for long with conversation A, pane B's
        Codex just started, bound or not; A starts a new conversation with a
        current creation time. Neither window's identity changes."""
        s, work = server
        self._bind_job(s, work, window="agent", job="old", conv="conv-a")
        assert self._conv(s, "agent") == "conv-a"
        s.tmux("set-option", "-w", "-t", "t:agent", "@agent_transcript", "/t/a.jsonl")
        fake = tmp_path / "bin" / "codex"
        s.tmux(
            "new-window", "-d", "-t", "t", "-n", "agent2", "-c", str(work), str(fake)
        )
        for _ in range(50):
            if (
                s.tmux(
                    "display-message", "-p", "-t", "t:agent2", "#{pane_current_command}"
                )
                == "codex"
            ):
                break
            time.sleep(0.1)
        for bound in (False, True):
            if bound:
                self._bind_job(s, work, window="agent2", job="other", conv="conv-b")
                assert self._conv(s, "agent2") == "conv-b"
            payload = self._start("conv-a-new", work)
            self._hook(s, self.INIT_SH, ["codex"], "%999", payload)
            self._hook(s, STATE_SH, ["idle"], "%999", payload)
            assert self._conv(s, "agent") == "conv-a"
            assert self._opt(s, "agent", "@agent_transcript") == "/t/a.jsonl"
            assert self._conv(s, "agent2") == ("conv-b" if bound else "")
            assert self._state(s, "agent2") == ("busy" if bound else "")
            assert self._state(s, "agent") == "busy"

    def test_a_session_outside_tmux_claims_no_window(self, server):
        """R1: an outside-tmux Codex session in the same directory, created
        just now like the pane's TUI: no window is claimed, at its start or
        at its later events."""
        s, work = server
        payload = self._start("conv-out", work)
        self._hook(s, self.INIT_SH, ["codex"], "%999", payload)
        assert self._conv(s, "agent") == ""
        turn = f'{{"session_id":"conv-out","cwd":"{work}","hook_event_name":"UserPromptSubmit","prompt":"hi"}}'
        self._hook(s, STATE_SH, ["busy"], "%999", turn)
        assert self._state(s, "agent") == ""

    def test_an_unmatched_payload_stamps_nothing(self, server):
        """F1: no window carries the conversation, no job names it, and the
        inherited TMUX_PANE (the daemon's) is live but foreign: the hook
        resolves to nothing rather than that pane."""
        s, work = server
        foreign = s.tmux("display-message", "-p", "-t", "t:far", "#{pane_id}")
        payload = '{"session_id":"conv-x","cwd":"/nowhere","hook_event_name":"Stop"}'
        self._hook(s, STATE_SH, ["done"], foreign, payload)
        for window in ("agent", "shell", "far"):
            assert self._state(s, window) == ""

    def test_a_jobs_first_prompt_binds_its_window_and_the_conversation_follows(
        self, server
    ):
        s, work = server
        self._bind_job(s, work)
        assert self._state(s, "agent") == "busy"
        assert self._conv(s, "agent") == "conv-j"
        stop = f'{{"session_id":"conv-j","cwd":"{work}","hook_event_name":"Stop"}}'
        self._hook(s, STATE_SH, ["done"], "%999", stop)
        assert self._state(s, "agent") == "done"
        assert self._state(s, "far") == "" and self._state(s, "shell") == ""
        # Another conversation's prompt for the same job: the window is
        # bound already, nothing changes.
        other = (
            f'{{"session_id":"conv-k","cwd":"{work}","hook_event_name":"UserPromptSubmit",'
            f'"prompt":"[Triggered by webhook: digest]\\n\\nAgain"}}'
        )
        self._hook(s, STATE_SH, ["busy"], "%999", other)
        assert self._state(s, "agent") == "done" and self._conv(s, "agent") == "conv-j"

    def test_a_job_prompt_binds_only_the_one_unbound_window_of_that_job(
        self, server, tmp_path
    ):
        """Two windows of the same job id (one finished and retained, one
        new) in the same directory: only an unbound one qualifies, and two
        unbound ones bind neither."""
        s, work = server
        fake = tmp_path / "bin" / "codex"
        s.tmux(
            "new-window", "-d", "-t", "t", "-n", "agent2", "-c", str(work), str(fake)
        )
        for window in ("agent", "agent2"):
            s.tmux("set-option", "-w", "-t", f"t:{window}", "@job_id", "digest")
            s.tmux("set-option", "-w", "-t", f"t:{window}", "@agent_cwd", str(work))
        self._hook(s, STATE_SH, ["busy"], "%999", self._job_prompt("conv-1", work))
        assert self._conv(s, "agent") == "" and self._conv(s, "agent2") == ""
        s.tmux("set-option", "-w", "-t", "t:agent", "@agent_conv", "conv-old")
        self._hook(s, STATE_SH, ["busy"], "%999", self._job_prompt("conv-2", work))
        assert self._conv(s, "agent") == "conv-old"
        assert (
            self._conv(s, "agent2") == "conv-2" and self._state(s, "agent2") == "busy"
        )

    def test_a_retained_failed_window_never_takes_a_retrys_binding(
        self, server, tmp_path
    ):
        """A run of the job failed before its first prompt (Codex exited at
        startup): its window stays, unbound, with @job_exit and @job_finished
        stamped. The retry's prompt binds only the retry's window, its Stop
        finishes only the retry, and the failed window keeps its state."""
        s, work = server
        failed = "agent"
        s.tmux("set-option", "-w", "-t", f"t:{failed}", "@job_id", "digest")
        s.tmux("set-option", "-w", "-t", f"t:{failed}", "@agent_cwd", str(work))
        s.tmux("set-option", "-w", "-t", f"t:{failed}", "@job_run", "run-failed")
        s.tmux("set-option", "-w", "-t", f"t:{failed}", "@job_exit", "1")
        s.tmux("set-option", "-w", "-t", f"t:{failed}", "@job_finished", "1700000000")
        s.tmux("set-option", "-w", "-t", f"t:{failed}", "@agent_state", "done")
        fake = tmp_path / "bin" / "codex"
        s.tmux("new-window", "-d", "-t", "t", "-n", "retry", "-c", str(work), str(fake))
        s.tmux("set-option", "-w", "-t", "t:retry", "@job_id", "digest")
        s.tmux("set-option", "-w", "-t", "t:retry", "@agent_cwd", str(work))
        s.tmux("set-option", "-w", "-t", "t:retry", "@job_run", "run-retry")
        self._hook(s, STATE_SH, ["busy"], "%999", self._job_prompt("conv-retry", work))
        assert (
            self._conv(s, "retry") == "conv-retry" and self._state(s, "retry") == "busy"
        )
        stop = f'{{"session_id":"conv-retry","cwd":"{work}","hook_event_name":"Stop"}}'
        self._hook(s, STATE_SH, ["done"], "%999", stop)
        assert self._state(s, "retry") == "done"
        assert self._conv(s, failed) == "" and self._state(s, failed) == "done"
        assert self._opt(s, failed, "@job_exit") == "1"

    def test_a_live_but_foreign_tmux_pane_is_not_trusted(self, server):
        """The daemon's TMUX_PANE may still exist (the pane that started it):
        a hook that does not descend from it must not stamp it."""
        s, work = server
        foreign = s.tmux("display-message", "-p", "-t", "t:far", "#{pane_id}")
        s.tmux("set-option", "-w", "-t", "t:agent", "@job_id", "digest")
        s.tmux("set-option", "-w", "-t", "t:agent", "@agent_cwd", str(work))
        self._hook(s, STATE_SH, ["busy"], foreign, self._job_prompt("conv-j", work))
        stop = f'{{"session_id":"conv-j","cwd":"{work}","hook_event_name":"Stop"}}'
        self._hook(s, STATE_SH, ["done"], foreign, stop)
        assert self._conv(s, "agent") == "conv-j" and self._state(s, "agent") == "done"
        assert self._state(s, "far") == "" and self._conv(s, "far") == ""

    def test_a_stale_tmux_socket_is_dropped(self, server, tmp_path):
        """$TMUX from a daemon born under a server that is gone must not
        stop the hook: tmux then reaches the default server."""
        s, work = server
        env = s.hook_env("%999")
        env["TMUX"] = f"{tmp_path}/gone.sock,1,0"
        env["TMUX_TMPDIR"] = str(tmp_path / "tt")
        (tmp_path / "tt" / f"tmux-{os.getuid()}").mkdir(parents=True, mode=0o700)
        os.symlink(s.socket, tmp_path / "tt" / f"tmux-{os.getuid()}" / "default")
        s.tmux("set-option", "-w", "-t", "t:agent", "@job_id", "digest")
        s.tmux("set-option", "-w", "-t", "t:agent", "@agent_cwd", str(work))
        subprocess.run(
            ["bash", str(STATE_SH), "busy"],
            input=self._job_prompt("conv-j", work),
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
            check=False,
        )
        assert self._state(s, "agent") == "busy" and self._conv(s, "agent") == "conv-j"


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
