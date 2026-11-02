"""Job runs in tmux windows, against a real, isolated tmux server.

Never the developer's server: ``TMUX_TMPDIR`` points tmux's default socket at
a private directory per test, so Merlin's bare ``tmux`` calls are isolated
with no seam. ``claude`` and ``codex`` on PATH are a fake agent that drives
``terminal/hooks/agent-state.sh`` and ``agent-session-init.sh`` the way the
real hooks do, and reads its scenario from the prompt. The agent configs the
hook check reads are temporary files under the test's home: the real
``~/.claude/settings.json`` and ``~/.codex/`` are never read or written.
"""

from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
import tmux_isolation

from board import sweep
from job import retention, runs, state, supervisor
from job import runner as job_runner

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)

JOBS = runs.JOBS_SESSION

# The fake agent. It stamps the window like the real hooks (identity at
# start, idle, then the scenario's states) and keeps running until killed,
# like a real agent that finished its turn and waits at its prompt.
FAKE_AGENT = """#!/bin/bash
H="$MERLIN_TERMINAL_HOOKS"
prompt="${@: -1}"
printf '%s' "$prompt" > "$FAKE_LOG/prompt.$(basename "$0")"
printf '%s\\n' "$@" > "$FAKE_LOG/argv.$(basename "$0")"
printf '{"session_id":"conv-1"}' | bash "$H/agent-session-init.sh" claude
bash "$H/agent-state.sh" idle </dev/null
scenario=$(printf '%s' "$prompt" | sed -n 's/.*scenario=\\([a-z-]*\\).*/\\1/p')
echo "fake agent got: $prompt"
case "$scenario" in
  stuck) sleep 1000 ;;
  crash) bash "$H/agent-state.sh" busy </dev/null; sleep 0.3; exit 2 ;;
  live) bash "$H/agent-state.sh" busy </dev/null; sleep 0.5
        bash "$H/agent-state.sh" idle </dev/null; sleep 1000 ;;
  hang) bash "$H/agent-state.sh" busy </dev/null; sleep 1000 ;;
  *) bash "$H/agent-state.sh" busy </dev/null; sleep 0.4
     echo "all done here"; bash "$H/agent-state.sh" done </dev/null; sleep 1000 ;;
esac
"""


@pytest.fixture
def iso(monkeypatch, tmp_path):
    """A private tmux socket, a fake agent on PATH, temporary agent configs
    carrying Merlin's hooks, and the job directories under tmp."""
    from lib import skills

    sockdir = Path(tempfile.mkdtemp(prefix="mj-"))  # short: unix socket limit
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "log"
    log.mkdir()
    for name in ("claude", "codex"):
        stub = bindir / name
        stub.write_text(FAKE_AGENT)
        stub.chmod(0o755)
    for var in ("TMUX", "TMUX_PANE", "MERLIN_SUPERVISED", "CLAUDECODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(sockdir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    # The hook check reads these temporary files, never the real ones.
    skills._install_hook_file(skills.claude_settings_path(), skills._CLAUDE_HOOK_EVENTS)
    skills._install_hook_file(skills.codex_hooks_path(), skills._CODEX_HOOK_EVENTS)

    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(job_runner, "JOBS_DIR", jobs_dir)
    monkeypatch.setattr(state, "JOBS_DIR", jobs_dir)
    monkeypatch.setattr(state, "STATE_DIR", jobs_dir / ".state")
    monkeypatch.setattr(state, "LOCKS_DIR", jobs_dir / ".locks")
    monkeypatch.setattr(state, "HISTORY_FILE", jobs_dir / ".history.json")
    from job import logs as job_logs

    monkeypatch.setattr(job_logs, "_logs_base_dir", lambda: tmp_path / "run-logs")
    # Fast polling: the rules are the same at 0.1 s as at 2 s.
    monkeypatch.setattr(supervisor, "POLL_INTERVAL", 0.1)
    monkeypatch.setattr(supervisor, "TERM_GRACE", 1.0)
    yield tmp_path, log
    tmux_isolation.kill_server(sockdir)
    shutil.rmtree(sockdir, ignore_errors=True)


def tmux(*args: str) -> str:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=False
    ).stdout.strip()


def windows() -> list[dict]:
    return (
        sweep.list_window_options(
            JOBS,
            (
                "window_name",
                "@job_id",
                "@job_run",
                "@agent_sid",
                "@agent_state",
                "@job_exit",
                "@job_finished",
            ),
        )
        or []
    )


def wait_until(pred, timeout: float = 10.0, step: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return pred()


def agent_job(scenario: str = "done", **over) -> dict:
    job = {"prompt": f"please run scenario={scenario} now", "enabled": True}
    job.update(over)
    return job


# ---------------------------------------------------------------------------
# Agent runs
# ---------------------------------------------------------------------------


def test_agent_run_from_launch_to_done(iso):
    tmp, log = iso
    result = job_runner.run_job("digest", agent_job("done"))
    assert result is not None
    assert result.exit_code == 0
    assert result.agent == "claude"

    rows = windows()
    assert len(rows) == 1, "one window, no stray shell"
    row = rows[0]
    assert row["window_name"] == "digest"
    assert row["@job_id"] == "digest"
    assert row["@job_run"] == result.run_id
    # Stamped before the agent's first hook: the session init kept our sid.
    assert row["@agent_sid"] == f"job-{result.run_id}"
    assert row["@job_exit"] == "0"
    assert row["@job_finished"].isdigit()
    assert result.window == f"{JOBS}:{row['window_id']}"

    # The agent keeps running: finishing a run does not end the conversation.
    assert tmux("display-message", "-p", "-t", result.window, "#{pane_dead}") == "0"

    # The prompt reached the agent verbatim, never through a command line.
    assert (log / "prompt.claude").read_text() == (
        "[Job: digest]\n\nplease run scenario=done now"
    )
    argv = (log / "argv.claude").read_text().splitlines()
    assert argv[0] == "--dangerously-skip-permissions"

    history = state.get_history("digest")
    assert len(history) == 1
    assert history[0]["exit_code"] == 0
    assert history[0]["agent"] == "claude"
    assert history[0]["run_id"] == result.run_id
    assert "all done here" in result.result  # the pane, as the record's output
    assert runs.is_recorded(tmp / "job-runs" / result.run_id)


def test_codex_job_uses_its_launch_line(iso):
    tmp, log = iso
    result = job_runner.run_job("cx", agent_job("done", agent="codex"))
    assert result is not None and result.exit_code == 0
    assert result.agent == "codex"
    argv = (log / "argv.codex").read_text().splitlines()
    assert argv[0] == "--dangerously-bypass-approvals-and-sandbox"
    assert state.get_history("cx")[0]["agent"] == "codex"


def test_run_watched_live_finishes_on_busy_then_idle(iso):
    """``done`` can be missed (the window selected, Esc): busy then idle is
    a finish."""
    result = job_runner.run_job("live", agent_job("live"))
    assert result is not None and result.exit_code == 0
    assert windows()[0]["@job_exit"] == "0"


def test_agent_crash_is_its_exit_code(iso):
    result = job_runner.run_job("boom", agent_job("crash"))
    assert result is not None and result.exit_code == 2
    # The supervisor saw the exit file the instant it was published; run.sh
    # stamps the window right after.
    assert wait_until(lambda: windows()[0]["@agent_state"] == "done", timeout=5)
    row = windows()[0]
    assert row["@job_exit"] == "2"
    # The window stays, on a shell.
    assert tmux("display-message", "-p", "-t", result.window, "#{pane_dead}") == "0"
    assert state.get_history("boom")[0]["exit_code"] == 2


def test_stuck_start_is_marked_asking_then_bounded_by_the_deadline(iso, monkeypatch):
    monkeypatch.setattr(supervisor, "START_WATCHDOG", 0.4)
    monkeypatch.setattr(job_runner, "AGENT_TIMEOUT_SECONDS", 2.5)
    seen: list[str] = []

    def watch():
        end = time.monotonic() + 6
        while time.monotonic() < end:
            rows = windows()
            if rows:
                seen.append(rows[0]["@agent_state"])
            time.sleep(0.05)

    t = threading.Thread(target=watch)
    t.start()
    result = job_runner.run_job("stuck", agent_job("stuck"))
    t.join()
    assert result is not None and result.exit_code == 124
    assert "ask" in seen, seen
    assert "deadline" in result.result
    assert runs.read_exit(runs.run_dirs()[0]) == 124


def test_deadline_ends_the_process_group_and_keeps_the_window(iso, monkeypatch):
    monkeypatch.setattr(job_runner, "AGENT_TIMEOUT_SECONDS", 2.0)
    result = job_runner.run_job("hang", agent_job("hang"))
    assert result is not None and result.exit_code == 124
    pgid = runs.read_pgid(runs.run_dirs()[0])
    assert pgid is not None
    assert wait_until(lambda: not supervisor._group_alive(pgid), timeout=5)
    row = windows()[0]
    assert row["@job_exit"] == "124"  # the supervisor's, written first
    assert wait_until(lambda: windows()[0]["@agent_state"] == "done", timeout=5)
    # The window stays readable: run.sh exec'd a shell.
    assert tmux("display-message", "-p", "-t", result.window, "#{pane_dead}") == "0"
    assert state.get_history("hang")[0]["exit_code"] == 124


def test_a_missing_hook_install_refuses_the_launch_with_no_window(iso):
    from lib import skills

    skills.claude_settings_path().write_text("{}")
    result = job_runner.run_job("nohooks", agent_job("done"))
    assert result is not None and result.exit_code == 1
    assert "merlin config agent-state-hooks auto" in result.result
    assert sweep.run_session_sweep_checked() in (None, [])
    assert runs.run_dirs() == []
    assert state.get_history("nohooks")[0]["exit_code"] == 1


def test_a_missing_binary_refuses_the_launch_with_127(iso, monkeypatch):
    from job import agents

    real_which = agents.shutil.which
    monkeypatch.setattr(
        agents.shutil,
        "which",
        lambda name: None if name == "claude" else real_which(name),
    )
    result = job_runner.run_job("nobin", agent_job("done"))
    assert result is not None and result.exit_code == 127
    assert "not found on PATH" in result.result


# ---------------------------------------------------------------------------
# Command runs
# ---------------------------------------------------------------------------


def test_command_with_a_failing_exit_code(iso):
    tmp, _ = iso
    job = {"type": "command", "command": "echo out; echo err >&2; exit 3"}
    result = job_runner.run_job("fail", job)
    assert result is not None and result.exit_code == 3
    assert "out" in result.result and "err" in result.result
    row = windows()[0]
    assert row["@job_exit"] == "3"
    assert row["@agent_state"] == "done"
    assert state.get_history("fail")[0]["exit_code"] == 3
    assert state.get_history("fail")[0]["agent"] is None
    assert runs.read_output(tmp / "job-runs" / result.run_id) == "out\nerr\n"
    # No job-control chatter in the window: its last lines (a notification's
    # snippet) are the job's own.
    pane = sweep.capture_pane(JOBS, row["window_id"], history=True) or ""
    assert "PIPESTATUS" not in pane and "fg:" not in pane
    assert pane.rstrip().splitlines()[-2:][0].strip() in ("out", "err") or "err" in pane


def test_command_stdin_is_closed_and_output_is_not_a_tty(iso):
    job = {
        "type": "command",
        "command": "if [ -t 1 ]; then echo tty; else echo notty; fi; "
        'read -r line && echo "got $line" || echo eof',
    }
    result = job_runner.run_job("stdin", job)
    assert result is not None and result.exit_code == 0
    assert "notty" in result.result
    assert "eof" in result.result


def test_job_reaches_the_same_merlin_whatever_server_it_was_born_under(
    iso, monkeypatch
):
    """The tmux server exists already, created with another MERLIN_HOME and
    CLAUDECODE set: the run still exports Merlin's home and the job
    variables, and CLAUDECODE is unset."""
    tmp, _ = iso
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", "other"],
        env={**os.environ, "MERLIN_HOME": "/elsewhere", "CLAUDECODE": "1"},
        check=True,
    )
    monkeypatch.setenv("MERLIN_HOME", str(tmp / "mh"))
    job = {
        "type": "command",
        "command": 'echo "home=$MERLIN_HOME id=$MERLIN_JOB_ID run=$MERLIN_JOB_RUN_ID '
        'trig=$MERLIN_JOB_TRIGGER cc=${CLAUDECODE:-unset}"',
    }
    result = job_runner.run_job("env", job, trigger="webhook")
    assert result is not None and result.exit_code == 0
    assert f"home={tmp / 'mh'}" in result.result
    assert "id=env" in result.result
    assert f"run={result.run_id}" in result.result
    assert "trig=webhook" in result.result
    assert "cc=unset" in result.result
    # The other session was not touched; merlin-jobs got exactly one window.
    assert {s.name for s in sweep.run_session_sweep()} == {"other", JOBS}
    assert len(windows()) == 1


def test_window_closed_mid_run_is_130(iso):
    job = {"type": "command", "command": "sleep 30"}
    out: dict = {}

    def run():
        out["result"] = job_runner.run_job("closed", job)

    t = threading.Thread(target=run)
    t.start()
    rows = wait_until(lambda: windows() or None, timeout=10)
    assert rows
    pgid = wait_until(lambda: runs.read_pgid(runs.run_dirs()[0]), timeout=10)
    assert pgid
    assert sweep.kill_window(JOBS, rows[0]["window_id"])
    t.join(timeout=15)
    assert not t.is_alive()
    result = out["result"]
    assert result.exit_code == 130
    assert "window closed before the run ended" in result.result
    assert state.get_history("closed")[0]["exit_code"] == 130
    # The job does not outlive its window.
    assert wait_until(lambda: not supervisor._group_alive(pgid), timeout=5)


def test_prompt_with_shell_metacharacters_reaches_the_agent_verbatim(iso):
    _, log = iso
    prompt = "say \"hi\" $(touch /tmp/never) `date` 'x' \\n and\nreal newline"
    result = job_runner.run_job("quote", {"prompt": prompt})
    assert result is not None and result.exit_code == 0
    assert (log / "prompt.claude").read_text() == f"[Job: quote]\n\n{prompt}"


# ---------------------------------------------------------------------------
# Orphans and overlapping ticks
# ---------------------------------------------------------------------------


def _launch_unsupervised(job_id: str, job: dict, deadline: float = 3600) -> runs.Run:
    run = runs.new_run(
        job_id,
        job,
        trigger="schedule",
        run_id=f"run-{job_id}",
        cwd=str(Path.home()),
        deadline=deadline,
    )
    runs.launch(run)
    return run


def test_orphan_finalized_once_under_overlapping_ticks(iso):
    """A run whose supervisor died (its exit file written by run.sh, no
    record): two ticks race to finalize it, one record results."""
    run = _launch_unsupervised(
        "orphan", {"type": "command", "command": "echo bye; exit 5"}
    )
    assert (
        wait_until(lambda: runs.read_exit(run.dir) is not None, timeout=10) is not None
    )

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def tick():
        barrier.wait()
        results.append(retention.tick()[0])

    threads = [threading.Thread(target=tick) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r["finalized"] for r in results) == 1
    history = state.get_history("orphan")
    assert len(history) == 1
    assert history[0]["exit_code"] == 5
    assert runs.is_recorded(run.dir)
    # A third tick records nothing more.
    assert retention.tick()[0]["finalized"] == 0
    assert len(state.get_history("orphan")) == 1


def test_orphan_with_its_window_gone_is_130(iso):
    run = _launch_unsupervised("gone", {"type": "command", "command": "sleep 30"})
    assert sweep.kill_window(JOBS, run.window_id)
    counts, adoptable = retention.tick()
    assert counts["finalized"] == 1 and adoptable == []
    assert state.get_history("gone")[0]["exit_code"] == 130
    # Recorded with no window: the directory goes at once.
    assert counts["removed"] == 1
    assert not run.dir.exists()
    assert retention.tick()[0] == {"finalized": 0, "closed": 0, "removed": 0}


def test_orphan_agent_run_is_adopted_and_finishes_on_done(iso):
    """A run still going on (no exit file, its window there) is not guessed
    at: the tick hands it to the dispatcher, which adopts it under the flock
    and supervises it to its end."""
    run = _launch_unsupervised("oagent", agent_job("done"))
    assert wait_until(lambda: windows()[0]["@agent_state"] == "done", timeout=10)
    counts, adoptable = retention.tick()
    assert counts["finalized"] == 0
    assert [r.run_id for r in adoptable] == [run.run_id]
    result = job_runner.adopt_run(adoptable[0])
    assert result is not None and result.exit_code == 0
    assert state.get_history("oagent")[0]["exit_code"] == 0
    assert runs.is_recorded(run.dir)
    assert retention.tick() == ({"finalized": 0, "closed": 0, "removed": 0}, [])


def test_restart_before_first_busy_marks_ask_and_bounds_the_run(iso, monkeypatch):
    """F9: a never-started agent (stuck on a dialog) whose supervisor died
    is not declared successful: the adopter's watchdog marks it asking and
    its deadline ends it."""
    monkeypatch.setattr(supervisor, "START_WATCHDOG", 0.4)
    run = _launch_unsupervised("stuck-orphan", agent_job("stuck"), deadline=2.5)
    time.sleep(0.6)  # idle for longer than the watchdog, nobody watching
    counts, adoptable = retention.tick()
    assert counts["finalized"] == 0 and [r.run_id for r in adoptable] == [run.run_id]
    seen: list[str] = []

    def watch():
        end = time.monotonic() + 6
        while time.monotonic() < end:
            rows = windows()
            if rows:
                seen.append(rows[0]["@agent_state"])
            time.sleep(0.05)

    t = threading.Thread(target=watch)
    t.start()
    result = job_runner.adopt_run(run)
    t.join()
    assert result is not None and result.exit_code == 124
    assert "ask" in seen
    assert state.get_history("stuck-orphan")[0]["exit_code"] == 124


def test_restart_after_busy_then_idle_finishes_the_adopted_run(iso):
    """F9: the first supervisor saw busy (stamped on the window) and died;
    the agent then went idle: the adopter finishes the run at once."""
    run = _launch_unsupervised("live-orphan", agent_job("live"))
    assert wait_until(lambda: windows()[0]["@agent_state"] == "busy", timeout=10)
    sweep.set_window_option(run.target, "@job_busy_seen", "1")  # the dead supervisor's
    assert wait_until(lambda: windows()[0]["@agent_state"] == "idle", timeout=10)
    result = job_runner.adopt_run(run)
    assert result is not None and result.exit_code == 0


def test_a_trigger_adopts_the_unfinished_run_instead_of_a_second_window(
    iso, monkeypatch
):
    """F4: the supervisor died while the agent works on; the next trigger of
    the job, by hand or by webhook, joins that run: one window, one run."""
    import asyncio

    from job import manage, webhook

    monkeypatch.setattr(job_runner, "AGENT_TIMEOUT_SECONDS", 2.0)
    pending = _launch_unsupervised("busy-orphan", agent_job("hang"), deadline=2.0)
    assert wait_until(lambda: windows()[0]["@agent_state"] == "busy", timeout=10)

    manage.JOBS_DIR = job_runner.JOBS_DIR
    job = {**agent_job("hang"), "webhook": {"secret": "whk_s"}}
    manage.save_job("busy-orphan", job)

    async def fire():
        first = await webhook._fire("busy-orphan", job)
        assert first.status == "coalesced"
        assert first.run_id == pending.run_id
        await asyncio.gather(*list(webhook._tasks))

    asyncio.run(fire())
    assert len(windows()) == 1
    history = state.get_history("busy-orphan")
    assert len(history) == 1 and history[0]["run_id"] == pending.run_id
    assert history[0]["exit_code"] == 124

    # A manual trigger with an unfinished run of its own job: the same.
    second = _launch_unsupervised("busy-orphan-2", agent_job("hang"), deadline=2.0)
    assert wait_until(lambda: len(windows()) == 2, timeout=10)
    result = job_runner.run_job("busy-orphan-2", agent_job("hang"))
    assert result is not None and result.run_id == second.run_id
    assert len(windows()) == 2


def test_orphan_recovery_never_touches_a_reused_window_id(iso):
    """F3: an old run directory points at a window id that now carries
    another run: the old run is 130, the window untouched."""
    live = _launch_unsupervised("live-job", {"type": "command", "command": "sleep 30"})
    old = runs.new_run(
        "old-job",
        {"type": "command", "command": "sleep 30"},
        trigger="manual",
        run_id="run-old",
        cwd=str(Path.home()),
        deadline=3600,
    )
    old.target = live.target  # the id, reused after a restart
    runs.write_meta(old)
    counts, adoptable = retention.tick()
    assert counts["finalized"] == 1 and [r.run_id for r in adoptable] == [live.run_id]
    assert state.get_history("old-job")[0]["exit_code"] == 130
    assert state.get_history("live-job") == []
    row = windows()[0]
    assert row["@job_run"] == live.run_id and row["@job_exit"] == ""
    assert row["@job_finished"] == ""
    assert runs.read_exit(live.dir) is None


def test_the_supervisor_finishes_on_a_reused_window_id_too(iso):
    """F3: the supervisor's own ownership check."""
    run = _launch_unsupervised("owner", {"type": "command", "command": "sleep 30"})
    assert sweep.set_window_option(run.target, "@job_run", "someone-else")
    status, opts = supervisor.observe(run)
    assert status == "gone" and opts is None
    assert supervisor.finish_from_window(run, status, opts, seen_busy=False) == 130
    sweep.set_window_option(run.target, "@job_run", run.run_id)
    assert supervisor.observe(run)[0] == "ok"


def test_unavailable_observations_finalize_close_and_signal_nothing(iso):
    """F6: a tmux that does not answer is not an absent window."""
    run = _launch_unsupervised("flaky", {"type": "command", "command": "exit 0"})
    assert (
        wait_until(lambda: runs.read_exit(run.dir) is not None, timeout=10) is not None
    )
    assert retention.tick()[0]["finalized"] == 1
    busy = _launch_unsupervised("flaky-busy", agent_job("hang"))
    assert wait_until(lambda: len(windows()) == 2, timeout=10)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sweep, "_tmux_observe", lambda args: ("unavailable", ""))
        counts, adoptable = retention.tick(now=time.time() + 3601)
        assert counts == {"finalized": 0, "closed": 0, "removed": 0}
        assert adoptable == []
        assert supervisor.observe(busy)[0] == "unavailable"
        assert (
            supervisor.finish_from_window(busy, "unavailable", None, seen_busy=True)
            is None
        )
    assert len(windows()) == 2 and run.dir.exists()

    # The client list alone unavailable: no window closed either.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sweep, "displayed_windows_checked", lambda: ("unavailable", set()))
        assert retention.tick(now=time.time() + 3601)[0]["closed"] == 0
    assert retention.tick(now=time.time() + 3601)[0]["closed"] == 1


def test_the_exit_is_first_writer_wins(iso):
    """F7: the command finished (run.sh published 3) as the deadline fired:
    the deadline path reads the winner and stamps it, nothing disagrees."""
    run = _launch_unsupervised("race", {"type": "command", "command": "exit 3"})
    assert wait_until(lambda: runs.read_exit(run.dir) == 3, timeout=10)
    code = supervisor.end_run(run, 124)
    assert code == 3
    assert runs.read_exit(run.dir) == 3
    assert windows()[0]["@job_exit"] == "3"
    outcome = supervisor.finish(run, code)
    job_runner.record_run(run, outcome)
    assert state.get_history("race")[0]["exit_code"] == 3


def test_write_exit_once_admits_one_writer(iso, tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def go(code):
        barrier.wait()
        wins.append(runs.write_exit_once(d, code))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1
    assert runs.read_exit(d) in range(8)
    assert [p.name for p in d.iterdir()] == ["exit"]


def test_two_first_launches_both_get_a_window(iso):
    """F8: two jobs dispatched together into an absent merlin-jobs: both
    open their window, one session, no stray shell."""
    results: dict[str, str | None] = {}
    barrier = threading.Barrier(2)

    def launch(job_id):
        barrier.wait()
        run = runs.new_run(
            job_id,
            {"type": "command", "command": "sleep 5"},
            trigger="manual",
            run_id=f"run-{job_id}",
            cwd=str(Path.home()),
            deadline=60,
        )
        try:
            results[job_id] = runs.launch(run)
        except runs.LaunchError as e:
            results[job_id] = None
            results[job_id + "-error"] = e.message

    threads = [threading.Thread(target=launch, args=(j,)) for j in ("first", "second")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results["first"] and results["second"], results
    assert {s.name for s in sweep.run_session_sweep()} == {JOBS}
    assert sorted(r["window_name"] for r in windows()) == ["first", "second"]


def test_supervised_run_is_left_alone_by_the_tick(iso):
    """The flock is held: the tick never finalizes a run someone watches."""
    job = {"type": "command", "command": "sleep 3"}
    out: dict = {}

    def run():
        out["result"] = job_runner.run_job("held", job)

    t = threading.Thread(target=run)
    t.start()
    wait_until(lambda: windows() or None, timeout=10)
    assert retention.tick()[0]["finalized"] == 0
    t.join(timeout=20)
    assert out["result"].exit_code == 0
    assert len(state.get_history("held")) == 1


def test_coalesced_webhook_fire_joins_the_running_window(iso):
    import asyncio

    from job import manage, webhook

    manage.JOBS_DIR = job_runner.JOBS_DIR
    job = {
        "type": "command",
        "command": "sleep 1.5; echo fired",
        "webhook": {"secret": "whk_s"},
        "enabled": True,
    }
    manage.save_job("hook", job)

    async def scenario():
        first = await webhook._fire("hook", job)
        await asyncio.sleep(0.3)
        second = await webhook._fire("hook", job)
        assert first.status == "launched"
        assert second.status == "coalesced"
        assert second.run_id == first.run_id
        await asyncio.gather(*list(webhook._tasks))
        return first.run_id

    run_id = asyncio.run(scenario())
    history = state.get_history("hook")
    assert len(history) == 1
    assert history[0]["run_id"] == run_id
    assert history[0]["trigger"] == "webhook"
    assert len(windows()) == 1


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _row(**over) -> dict:
    row = {
        "window_id": "@9",
        "@job_run": "r1",
        "@job_id": "j",
        "@job_exit": "0",
        "@job_finished": "1000",
        "@job_started": "900",
        "window_activity": "1001",
    }
    row.update(over)
    return row


class TestShouldClose:
    def test_displayed_window_is_never_closed(self):
        row = _row(**{"@job_finished": "0"})
        assert not retention.should_close(
            row, now=10**6, displayed={f"{JOBS}:@9"}, newer_run_started=None
        )

    def test_success_closes_after_an_hour(self):
        assert not retention.should_close(
            _row(), now=1000 + 3599, displayed=set(), newer_run_started=None
        )
        assert retention.should_close(
            _row(), now=1000 + 3600, displayed=set(), newer_run_started=None
        )

    def test_success_closes_at_the_next_run_of_the_job(self):
        assert retention.should_close(
            _row(), now=1100, displayed=set(), newer_run_started=1050
        )
        assert not retention.should_close(
            _row(), now=1100, displayed=set(), newer_run_started=950
        )

    def test_failure_waits_a_day_after_the_last_activity(self):
        row = _row(**{"@job_exit": "3", "window_activity": "2000"})
        assert not retention.should_close(
            row, now=2000 + 86399, displayed=set(), newer_run_started=1050
        )
        assert retention.should_close(
            row, now=2000 + 86400, displayed=set(), newer_run_started=None
        )

    def test_stepping_in_keeps_a_success_for_a_day(self):
        row = _row(window_activity="1500")  # someone typed after the finish
        assert not retention.should_close(
            row, now=1000 + 3600, displayed=set(), newer_run_started=1050
        )
        assert retention.should_close(
            row, now=1500 + 86400, displayed=set(), newer_run_started=None
        )

    def test_the_finish_own_output_is_not_stepping_in(self):
        row = _row(window_activity="1004")  # within the slack
        assert retention.should_close(
            row, now=1000 + 3600, displayed=set(), newer_run_started=None
        )

    def test_unfinished_window_is_kept(self):
        row = _row(**{"@job_finished": "", "@job_exit": ""})
        assert not retention.should_close(
            row, now=10**7, displayed=set(), newer_run_started=None
        )


def test_tick_closes_windows_past_retention_and_spares_the_rest(iso):
    """A success closes an hour after its finish, a failure a day after its
    last activity; a plain window and another session are never touched."""
    ok = _launch_unsupervised("ok", {"type": "command", "command": "exit 0"})
    failed = _launch_unsupervised("failed", {"type": "command", "command": "exit 3"})
    for run in (ok, failed):
        assert (
            wait_until(lambda r=run: runs.read_exit(r.dir) is not None, timeout=10)
            is not None
        )
    # Both finalized now (recorded, @job_finished stamped).
    assert retention.tick()[0]["finalized"] == 2
    # A plain window in merlin-jobs and another session must never be touched.
    tmux("new-window", "-d", "-t", f"{JOBS}:", "-n", "plain")
    tmux("new-session", "-d", "-s", "mine")

    assert retention.tick()[0]["closed"] == 0  # nothing is an hour old yet
    counts, _ = retention.tick(now=time.time() + 3601)
    assert counts["closed"] == 1 and counts["removed"] == 1
    assert {r["window_name"] for r in windows()} == {"failed", "plain"}
    assert not ok.dir.exists() and failed.dir.exists()
    assert "mine" in {s.name for s in sweep.run_session_sweep()}

    counts, _ = retention.tick(now=time.time() + 86401)
    assert counts["closed"] == 1 and counts["removed"] == 1
    assert {r["window_name"] for r in windows()} == {"plain"}
    assert not failed.dir.exists()


def test_success_closes_at_the_next_run_of_the_job(iso):
    first = _launch_unsupervised("same", {"type": "command", "command": "exit 0"})
    assert (
        wait_until(lambda: runs.read_exit(first.dir) is not None, timeout=10)
        is not None
    )
    assert retention.tick()[0]["finalized"] == 1
    assert retention.tick()[0]["closed"] == 0
    # The next run of the same job opens, a whole second later.
    time.sleep(1.1)
    second = runs.new_run(
        "same",
        {"type": "command", "command": "sleep 30"},
        trigger="manual",
        run_id="run-same-2",
        cwd=str(Path.home()),
        deadline=3600,
    )
    runs.launch(second)
    counts, _ = retention.tick()
    assert counts["closed"] == 1
    assert [r["@job_run"] for r in windows()] == ["run-same-2"]
    assert not first.dir.exists() and second.dir.exists()


def test_a_window_a_client_is_showing_is_never_closed(iso):
    old = _launch_unsupervised("shown", {"type": "command", "command": "exit 0"})
    assert (
        wait_until(lambda: runs.read_exit(old.dir) is not None, timeout=10) is not None
    )
    assert retention.tick()[0]["finalized"] == 1
    later = time.time() + 3601

    # A client on that window: tmux attach under a pty.
    master, slave = pty.openpty()
    client = subprocess.Popen(
        ["tmux", "attach", "-t", old.target],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm"},
    )
    try:
        assert wait_until(lambda: old.target in sweep.displayed_windows(), timeout=10)
        assert retention.tick(now=later)[0]["closed"] == 0
        assert old.dir.exists()
    finally:
        client.terminate()
        client.wait(timeout=5)
        os.close(master)
        os.close(slave)
    assert wait_until(lambda: old.target not in sweep.displayed_windows(), timeout=10)
    # Nobody is looking any more: the ordinary rule applies.
    assert retention.tick(now=later)[0]["closed"] == 1


def test_tick_with_nothing_to_do_touches_no_tmux(iso, monkeypatch):
    calls: list = []
    monkeypatch.setattr(sweep, "displayed_windows", lambda: calls.append(1) or set())
    assert retention.tick()[0] == {"finalized": 0, "closed": 0, "removed": 0}
    assert calls == []


# ---------------------------------------------------------------------------
# The workspace never sees merlin-jobs
# ---------------------------------------------------------------------------


def test_workspace_snapshot_and_restore_skip_merlin_jobs():
    from workspace import restore as rs
    from workspace import snapshot as snap

    class NoProc(snap.Proc):
        def children(self):
            return {}

        def argv(self, pid):
            return []

        def stat(self, pid):
            return None

        def cwd(self, pid):
            return None

    sessions = [{"session_name": "dev"}, {"session_name": JOBS}]
    windows_rows = [
        {
            "session_name": name,
            "window_id": f"@{i}",
            "window_index": "0",
            "window_name": "w",
            "window_active": "1",
            "window_layout": "x",
            "automatic-rename": "1",
            "@agent_sid": "",
            "@agent_cwd": "",
            "@agent_parent": "",
            "@agent_relation": "",
            "@agent_provider": "",
            "@agent_conv": "",
        }
        for i, name in enumerate(("dev", JOBS))
    ]
    panes = [
        {
            "window_id": f"@{i}",
            "pane_index": "0",
            "pane_current_path": "/tmp",
            "pane_active": "1",
            "pane_pid": "1",
            "pane_current_command": "bash",
        }
        for i in range(2)
    ]
    shot = snap.build_snapshot(
        sessions, windows_rows, panes, proc=NoProc(), claude_by_pid={}
    )
    assert [s["name"] for s in shot["sessions"]] == ["dev"]

    # An older snapshot that recorded the session: never restored.
    old = {"version": 1, "saved_at": 0, "sessions": [{"name": JOBS, "windows": [{}]}]}
    assert rs.missing_sessions(old, {}) == []


def test_job_meta_round_trip(iso):
    run = runs.new_run(
        "meta",
        {"type": "command", "command": "true", "report_mode": "silent"},
        trigger="webhook",
        run_id="r-meta",
        cwd="/tmp",
        deadline=10,
    )
    back = runs.read_meta(run.dir)
    assert back is not None
    assert back.to_meta() == run.to_meta()
    assert back.report_mode == "silent" and back.trigger == "webhook"
    assert oct(run.dir.stat().st_mode & 0o777) == "0o700"
    assert json.loads((run.dir / "meta.json").read_text())["run_id"] == "r-meta"


# ---------------------------------------------------------------------------
# Review round two
# ---------------------------------------------------------------------------


def test_the_server_exiting_under_a_read_is_absence(monkeypatch):
    """tmux says "server exited unexpectedly" when the server it reached was
    closing (its last window killed): nothing on it exists any more."""
    import subprocess as sp

    def fake_run(cmd, **kw):
        return sp.CompletedProcess(
            cmd, 1, stdout="", stderr="server exited unexpectedly\n"
        )

    monkeypatch.setattr(sweep.subprocess, "run", fake_run)
    assert sweep._tmux_observe(["list-windows"]) == ("gone", "")

    def refused(cmd, **kw):
        return sp.CompletedProcess(
            cmd, 1, stdout="", stderr="error connecting to x (Connection refused)\n"
        )

    monkeypatch.setattr(sweep.subprocess, "run", refused)
    assert sweep._tmux_observe(["list-windows"]) == ("unavailable", "")


def test_a_launch_interrupted_after_the_window_is_recovered_by_its_run_id(iso):
    """R2: the launch crashed after stamping the window's identity but before
    the metadata got its target: the recovery attributes the window by
    @job_run and the run goes on (here to its exit)."""
    run = runs.new_run(
        "cut",
        {"type": "command", "command": "exit 0"},
        trigger="manual",
        run_id="run-cut",
        cwd=str(Path.home()),
        deadline=60,
    )
    wid = sweep.open_window(JOBS, "cut", run.cwd, f"bash {run.dir / 'run.sh'}")
    assert wid
    assert sweep.set_window_options(
        f"{JOBS}:{wid}", {"@job_id": "cut", "@job_run": "run-cut"}
    )
    assert runs.read_meta(run.dir).target == ""  # the crash point
    assert runs.unfinished_run("cut") is not None  # a trigger would adopt it
    assert runs.read_meta(run.dir).target == f"{JOBS}:{wid}"
    # The gate was never stamped: run.sh gives up, publishes exit 1, leaves.
    run.target = ""
    runs.write_meta(run)
    counts, adoptable = retention.tick()
    assert counts["finalized"] == 0 and [r.run_id for r in adoptable] == ["run-cut"]
    assert runs.read_meta(run.dir).target == f"{JOBS}:{wid}"


def test_a_launch_interrupted_before_the_window_is_a_failed_run(iso):
    """R2: no window carries the run id and the preparation is old: recorded
    as a failed launch, never adopted, never a second window's excuse."""
    run = runs.new_run(
        "never",
        {"type": "command", "command": "exit 0"},
        trigger="manual",
        run_id="run-never",
        cwd=str(Path.home()),
        deadline=60,
    )
    assert runs.unfinished_run("never") is None
    counts, adoptable = retention.tick()
    assert counts["finalized"] == 0 and adoptable == []  # too young to judge
    old = runs.read_meta(run.dir)
    old.started -= runs.PREPARATION_TTL + 1
    runs.write_meta(old)
    counts, adoptable = retention.tick()
    assert counts["finalized"] == 1 and adoptable == []
    assert state.get_history("never")[0]["exit_code"] == 1
    assert counts["removed"] == 1 and not run.dir.exists()  # recorded, no window
    assert runs.unfinished_run("never") is None


def test_gate_timeout_publishes_exit_one_and_closes_the_window(iso, monkeypatch):
    """R2: a window whose options Merlin never stamped does not linger."""
    monkeypatch.setattr(runs, "STAMP_WAIT_SECONDS", 1)
    run = runs.new_run(
        "gate",
        {"type": "command", "command": "exit 0"},
        trigger="manual",
        run_id="run-gate",
        cwd=str(Path.home()),
        deadline=60,
    )
    wid = sweep.open_window(JOBS, "gate", run.cwd, f"bash {run.dir / 'run.sh'}")
    assert wid
    assert wait_until(lambda: runs.read_exit(run.dir) == 1, timeout=10)
    assert wait_until(lambda: not windows(), timeout=10)


def test_an_unrecorded_outcome_survives_the_windows_retention(iso, monkeypatch):
    """R3: the run log keeps failing to write; the window closes at its TTL
    but the directory with the canonical outcome stays, and once writes work
    the record is materialized exactly once."""
    from job import logs as job_logs

    real_write_log = job_logs.write_log
    broken = {"on": True}

    def flaky(job_id, entry):
        if broken["on"]:
            raise OSError("disk full")
        return real_write_log(job_id, entry)

    monkeypatch.setattr(job_logs, "write_log", flaky)
    run = _launch_unsupervised("keep", {"type": "command", "command": "exit 0"})
    assert (
        wait_until(lambda: runs.read_exit(run.dir) is not None, timeout=10) is not None
    )
    assert retention.tick()[0]["finalized"] == 1
    assert runs.read_outcome(run.dir) is not None and not runs.is_recorded(run.dir)
    counts, _ = retention.tick(now=time.time() + 3601)
    assert counts["closed"] == 1 and counts["removed"] == 0
    assert run.dir.exists() and windows() == []
    broken["on"] = False
    counts, _ = retention.tick()
    assert counts["finalized"] == 1
    assert counts["removed"] == 1 and not run.dir.exists()  # recorded, no window
    assert len(state.get_history("keep")) == 1
    assert len(job_logs.list_logs("keep")) == 1


def test_an_interrupted_deadline_is_finished_by_the_recovery(iso):
    """R4: the supervisor published 124 and died before its signals; the job
    ignores SIGTERM. The recovery ends the verified group (SIGKILL after the
    grace) before it records the run, so nothing outlives the record."""
    run = _launch_unsupervised(
        "stubborn", {"type": "command", "command": "trap '' TERM; sleep 100"}
    )
    pgid = wait_until(lambda: runs.read_pgid(run.dir), timeout=10)
    assert pgid
    time.sleep(0.5)
    runs.mark_terminating(run.dir)  # the dead supervisor's intent
    assert runs.write_exit_once(run.dir, 124)
    supervisor.TERM_GRACE = 0.5
    try:
        counts, adoptable = retention.tick()
    finally:
        supervisor.TERM_GRACE = 5.0
    assert counts["finalized"] == 1 and adoptable == []
    assert not supervisor._group_alive(pgid)
    assert state.get_history("stubborn")[0]["exit_code"] == 124


def test_end_run_retried_after_publication_still_ends_the_group(iso):
    """R4: end_run called again when its exit already stands (a supervisor's
    retry) signals the group instead of assuming run.sh ended it."""
    run = _launch_unsupervised(
        "again", {"type": "command", "command": "trap '' TERM; sleep 100"}
    )
    pgid = wait_until(lambda: runs.read_pgid(run.dir), timeout=10)
    assert pgid
    time.sleep(0.5)
    runs.mark_terminating(run.dir)
    assert runs.write_exit_once(run.dir, 124)
    assert supervisor.end_run(run, 124, term_grace=0.5) == 124
    assert not supervisor._group_alive(pgid)


def test_a_webhook_fire_keeps_the_loop_free_and_single_flight(iso, monkeypatch):
    """R5: the unfinished-run lookup runs off the loop; a stalled lookup
    stalls nothing else, and two fires during it get one run."""
    import asyncio

    from job import manage, webhook

    manage.JOBS_DIR = job_runner.JOBS_DIR
    job = {"type": "command", "command": "sleep 2", "webhook": {"secret": "whk_s"}}
    manage.save_job("free", job)
    real = runs.unfinished_run

    def slow(job_id):
        time.sleep(0.6)
        return real(job_id)

    monkeypatch.setattr(runs, "unfinished_run", slow)

    async def scenario():
        beats = {"n": 0}

        async def heartbeat():
            while True:
                beats["n"] += 1
                await asyncio.sleep(0.05)

        hb = asyncio.create_task(heartbeat())
        first, second = await asyncio.gather(
            webhook._fire("free", job), webhook._fire("free", job)
        )
        hb.cancel()
        assert beats["n"] >= 5, beats  # the loop kept turning during the lookup
        assert {first.status, second.status} == {"launched", "coalesced"}
        assert first.run_id == second.run_id
        await asyncio.gather(*list(webhook._tasks))
        return first.run_id

    run_id = asyncio.run(scenario())
    assert len(windows()) == 1
    assert [e["run_id"] for e in state.get_history("free")] == [run_id]


def test_activity_after_the_sweep_keeps_the_window(iso):
    """R6: someone steps into a success window between the sweep and the
    kill: the final read sees the new activity and keeps it."""
    run = _launch_unsupervised("late", {"type": "command", "command": "exit 0"})
    assert (
        wait_until(lambda: runs.read_exit(run.dir) is not None, timeout=10) is not None
    )
    assert retention.tick()[0]["finalized"] == 1
    # The finish is a while behind us: anything typed now is a step-in.
    assert sweep.set_window_option(
        run.target, "@job_finished", str(int(time.time()) - 60)
    )
    later = time.time() + 3601
    real = sweep.displayed_windows_checked
    calls = {"n": 0}

    def step_in_then_answer():
        calls["n"] += 1
        if calls["n"] == 1:

            def activity():
                rows = sweep.list_window_options(JOBS, ("window_activity",)) or []
                return rows[0]["window_activity"] if rows else ""

            before = activity()
            # Typed into the window after the sweep: a human stepping in.
            sweep.send_literal(run.target, "echo stepped in")
            sweep.send_enter(run.target)
            wait_until(lambda: activity() != before, timeout=10)
        return real()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sweep, "displayed_windows_checked", step_in_then_answer)
        counts, _ = retention.tick(now=later)
    assert counts["closed"] == 0
    assert len(windows()) == 1
    assert retention.tick(now=time.time() + 86401 + 10)[0]["closed"] == 1
