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
    result = job_runner.run_job("digest", agent_job("done"), emit_result=False)
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
    result = job_runner.run_job(
        "cx", agent_job("done", agent="codex"), emit_result=False
    )
    assert result is not None and result.exit_code == 0
    assert result.agent == "codex"
    argv = (log / "argv.codex").read_text().splitlines()
    assert argv[0] == "--dangerously-bypass-approvals-and-sandbox"
    assert state.get_history("cx")[0]["agent"] == "codex"


def test_run_watched_live_finishes_on_busy_then_idle(iso):
    """``done`` can be missed (the window selected, Esc): busy then idle is
    a finish."""
    result = job_runner.run_job("live", agent_job("live"), emit_result=False)
    assert result is not None and result.exit_code == 0
    assert windows()[0]["@job_exit"] == "0"


def test_agent_crash_is_its_exit_code(iso):
    result = job_runner.run_job("boom", agent_job("crash"), emit_result=False)
    assert result is not None and result.exit_code == 2
    row = windows()[0]
    assert row["@job_exit"] == "2"
    assert row["@agent_state"] == "done"  # run.sh stamped the end
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
    result = job_runner.run_job("stuck", agent_job("stuck"), emit_result=False)
    t.join()
    assert result is not None and result.exit_code == 124
    assert "ask" in seen, seen
    assert "deadline" in result.result
    assert runs.read_exit(runs.run_dirs()[0]) == 124


def test_deadline_ends_the_process_group_and_keeps_the_window(iso, monkeypatch):
    monkeypatch.setattr(job_runner, "AGENT_TIMEOUT_SECONDS", 1.0)
    result = job_runner.run_job("hang", agent_job("hang"), emit_result=False)
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
    result = job_runner.run_job("nohooks", agent_job("done"), emit_result=False)
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
    result = job_runner.run_job("nobin", agent_job("done"), emit_result=False)
    assert result is not None and result.exit_code == 127
    assert "not found on PATH" in result.result


# ---------------------------------------------------------------------------
# Command runs
# ---------------------------------------------------------------------------


def test_command_with_a_failing_exit_code(iso):
    tmp, _ = iso
    job = {"type": "command", "command": "echo out; echo err >&2; exit 3"}
    result = job_runner.run_job("fail", job, emit_result=False)
    assert result is not None and result.exit_code == 3
    assert "out" in result.result and "err" in result.result
    row = windows()[0]
    assert row["@job_exit"] == "3"
    assert row["@agent_state"] == "done"
    assert state.get_history("fail")[0]["exit_code"] == 3
    assert state.get_history("fail")[0]["agent"] is None
    assert runs.read_output(tmp / "job-runs" / result.run_id) == "out\nerr\n"


def test_command_stdin_is_closed_and_output_is_not_a_tty(iso):
    job = {
        "type": "command",
        "command": "if [ -t 1 ]; then echo tty; else echo notty; fi; "
        'read -r line && echo "got $line" || echo eof',
    }
    result = job_runner.run_job("stdin", job, emit_result=False)
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
    result = job_runner.run_job("env", job, emit_result=False, trigger="webhook")
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
        out["result"] = job_runner.run_job("closed", job, emit_result=False)

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
    result = job_runner.run_job("quote", {"prompt": prompt}, emit_result=False)
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
        results.append(retention.tick())

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
    assert retention.tick()["finalized"] == 0
    assert len(state.get_history("orphan")) == 1


def test_orphan_with_its_window_gone_is_130(iso):
    run = _launch_unsupervised("gone", {"type": "command", "command": "sleep 30"})
    assert sweep.kill_window(JOBS, run.window_id)
    counts = retention.tick()
    assert counts["finalized"] == 1
    assert state.get_history("gone")[0]["exit_code"] == 130
    # Recorded with no window: the directory goes at once.
    assert counts["removed"] == 1
    assert not run.dir.exists()
    assert retention.tick() == {"finalized": 0, "closed": 0, "removed": 0}


def test_orphan_agent_run_finishes_on_done(iso):
    _launch_unsupervised("oagent", agent_job("done"))
    assert wait_until(lambda: windows()[0]["@agent_state"] == "done", timeout=10)
    assert retention.tick()["finalized"] == 1
    assert state.get_history("oagent")[0]["exit_code"] == 0


def test_supervised_run_is_left_alone_by_the_tick(iso):
    """The flock is held: the tick never finalizes a run someone watches."""
    job = {"type": "command", "command": "sleep 3"}
    out: dict = {}

    def run():
        out["result"] = job_runner.run_job("held", job, emit_result=False)

    t = threading.Thread(target=run)
    t.start()
    wait_until(lambda: windows() or None, timeout=10)
    assert retention.tick()["finalized"] == 0
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
        first = webhook._fire("hook", job)
        await asyncio.sleep(0.3)
        second = webhook._fire("hook", job)
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
    assert retention.tick()["finalized"] == 2
    # A plain window in merlin-jobs and another session must never be touched.
    tmux("new-window", "-d", "-t", f"{JOBS}:", "-n", "plain")
    tmux("new-session", "-d", "-s", "mine")

    assert retention.tick()["closed"] == 0  # nothing is an hour old yet
    counts = retention.tick(now=time.time() + 3601)
    assert counts["closed"] == 1 and counts["removed"] == 1
    assert {r["window_name"] for r in windows()} == {"failed", "plain"}
    assert not ok.dir.exists() and failed.dir.exists()
    assert "mine" in {s.name for s in sweep.run_session_sweep()}

    counts = retention.tick(now=time.time() + 86401)
    assert counts["closed"] == 1 and counts["removed"] == 1
    assert {r["window_name"] for r in windows()} == {"plain"}
    assert not failed.dir.exists()


def test_success_closes_at_the_next_run_of_the_job(iso):
    first = _launch_unsupervised("same", {"type": "command", "command": "exit 0"})
    assert (
        wait_until(lambda: runs.read_exit(first.dir) is not None, timeout=10)
        is not None
    )
    assert retention.tick()["finalized"] == 1
    assert retention.tick()["closed"] == 0
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
    counts = retention.tick()
    assert counts["closed"] == 1
    assert [r["@job_run"] for r in windows()] == ["run-same-2"]
    assert not first.dir.exists() and second.dir.exists()


def test_a_window_a_client_is_showing_is_never_closed(iso):
    old = _launch_unsupervised("shown", {"type": "command", "command": "exit 0"})
    assert (
        wait_until(lambda: runs.read_exit(old.dir) is not None, timeout=10) is not None
    )
    assert retention.tick()["finalized"] == 1
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
        assert retention.tick(now=later)["closed"] == 0
        assert old.dir.exists()
    finally:
        client.terminate()
        client.wait(timeout=5)
        os.close(master)
        os.close(slave)
    assert wait_until(lambda: old.target not in sweep.displayed_windows(), timeout=10)
    # Nobody is looking any more: the ordinary rule applies.
    assert retention.tick(now=later)["closed"] == 1


def test_tick_with_nothing_to_do_touches_no_tmux(iso, monkeypatch):
    calls: list = []
    monkeypatch.setattr(sweep, "displayed_windows", lambda: calls.append(1) or set())
    assert retention.tick() == {"finalized": 0, "closed": 0, "removed": 0}
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
