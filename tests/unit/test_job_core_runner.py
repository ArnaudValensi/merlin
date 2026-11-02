"""Tests for job.runner — the job dispatcher."""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# croniter is needed for testing - install with: uv pip install croniter
pytest.importorskip("croniter")


@pytest.fixture
def temp_jobs_dir(tmp_path):
    """Fixture that patches job.runner and job.state to use a temporary directory."""
    from job import runner as job_runner
    from job import state as job_state

    # Save originals
    orig = {
        "runner_dir": job_runner.JOBS_DIR,
        "state_dir": job_state.STATE_DIR,
        "locks_dir": job_state.LOCKS_DIR,
        "history_file": job_state.HISTORY_FILE,
    }

    # Patch all paths
    job_runner.JOBS_DIR = tmp_path
    job_state.STATE_DIR = tmp_path / ".state"
    job_state.LOCKS_DIR = tmp_path / ".locks"
    job_state.HISTORY_FILE = tmp_path / ".history.json"

    yield tmp_path

    # Restore
    job_runner.JOBS_DIR = orig["runner_dir"]
    job_state.STATE_DIR = orig["state_dir"]
    job_state.LOCKS_DIR = orig["locks_dir"]
    job_state.HISTORY_FILE = orig["history_file"]


def create_job_file(directory: Path, job_id: str, **overrides) -> Path:
    """Helper to create a job file with defaults."""
    job = {
        "description": f"Test job {job_id}",
        "schedule": "0 9 * * *",
        "prompt": f"Test prompt for {job_id}",
        "channel": "123456789",
        "enabled": True,
        "report_mode": "always",
        "created_at": "2026-02-05T00:00:00Z",
    }
    job.update(overrides)

    path = directory / f"{job_id}.json"
    path.write_text(json.dumps(job))
    return path


class TestLoadJob:
    """Tests for load_job function."""

    def test_load_valid_job(self, temp_jobs_dir):
        """load_job returns job data for valid JSON."""
        from job.runner import load_job

        path = create_job_file(temp_jobs_dir, "test-job")
        job = load_job(path)
        assert job is not None
        assert job["description"] == "Test job test-job"
        assert job["schedule"] == "0 9 * * *"

    def test_load_job_invalid_json(self, temp_jobs_dir):
        """load_job returns None for invalid JSON."""
        from job.runner import load_job

        path = temp_jobs_dir / "bad.json"
        path.write_text("not valid json")
        assert load_job(path) is None

    def test_load_job_missing_required_field(self, temp_jobs_dir):
        """load_job returns None when required fields are missing."""
        from job.runner import load_job

        path = temp_jobs_dir / "missing.json"
        path.write_text(
            json.dumps({"description": "test"})
        )  # missing schedule, prompt, channel
        assert load_job(path) is None

    def test_load_job_invalid_cron_expression(self, temp_jobs_dir):
        """load_job returns None for invalid cron expression."""
        from job.runner import load_job

        path = create_job_file(temp_jobs_dir, "bad-cron", schedule="invalid cron")
        assert load_job(path) is None


class TestLoadAllJobs:
    """Tests for load_all_jobs function."""

    def test_load_all_jobs_empty(self, temp_jobs_dir):
        """load_all_jobs returns empty dict when no jobs exist."""
        from job.runner import load_all_jobs

        assert load_all_jobs() == {}

    def test_load_all_jobs_multiple(self, temp_jobs_dir):
        """load_all_jobs loads all valid job files."""
        from job.runner import load_all_jobs

        create_job_file(temp_jobs_dir, "job1")
        create_job_file(temp_jobs_dir, "job2")

        jobs = load_all_jobs()
        assert len(jobs) == 2
        assert "job1" in jobs
        assert "job2" in jobs

    def test_load_all_jobs_skips_dotfiles(self, temp_jobs_dir):
        """load_all_jobs skips files starting with dot."""
        from job.runner import load_all_jobs

        create_job_file(temp_jobs_dir, "job1")
        (temp_jobs_dir / ".state.json").write_text("{}")
        (temp_jobs_dir / ".history.json").write_text("{}")

        jobs = load_all_jobs()
        assert len(jobs) == 1
        assert "job1" in jobs

    def test_load_all_jobs_skips_templates(self, temp_jobs_dir):
        """load_all_jobs skips files starting with underscore."""
        from job.runner import load_all_jobs

        create_job_file(temp_jobs_dir, "job1")
        (temp_jobs_dir / "_example.json.template").write_text("{}")

        jobs = load_all_jobs()
        assert len(jobs) == 1
        assert "job1" in jobs

    def test_load_all_jobs_skips_invalid(self, temp_jobs_dir):
        """load_all_jobs skips invalid job files gracefully."""
        from job.runner import load_all_jobs

        create_job_file(temp_jobs_dir, "valid-job")
        (temp_jobs_dir / "invalid.json").write_text("not json")

        jobs = load_all_jobs()
        assert len(jobs) == 1
        assert "valid-job" in jobs


class TestIsJobDue:
    """Tests for is_job_due function."""

    def test_never_seen_not_due(self, temp_jobs_dir):
        """Never-seen job is NOT due (guard initializes state)."""
        from job.runner import is_job_due

        now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
        assert is_job_due("new-job", "0 9 * * *", now) is False

    def test_never_seen_initializes_state(self, temp_jobs_dir):
        """Never-seen job gets its state initialized to now."""
        from job.runner import is_job_due
        from job.state import get_last_run

        now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
        is_job_due("new-job", "0 9 * * *", now)

        last_run = get_last_run("new-job")
        assert last_run == now

    def test_job_due_after_schedule(self, temp_jobs_dir):
        """Job is due when scheduled time has passed since last run."""
        from job.runner import is_job_due
        from job.state import set_last_run

        # Last run was yesterday at 9:00
        last_run = datetime(2026, 2, 4, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # Now is today at 10:00 (after 9:00 schedule, within 15 min grace)
        now = datetime(2026, 2, 5, 9, 5, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now) is True

    def test_job_not_due_before_schedule(self, temp_jobs_dir):
        """Job is not due when scheduled time hasn't passed."""
        from job.runner import is_job_due
        from job.state import set_last_run

        # Last run was today at 9:00
        last_run = datetime(2026, 2, 5, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # Now is today at 9:30 (before next 9:00)
        now = datetime(2026, 2, 5, 9, 30, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now) is False

    def test_job_not_due_immediately_after_run(self, temp_jobs_dir):
        """Job is not due immediately after it was run."""
        from job.runner import is_job_due
        from job.state import set_last_run

        now = datetime(2026, 2, 5, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", now)

        # Even with "every minute" schedule, not due at same minute
        assert is_job_due("test-job", "* * * * *", now) is False

    def test_stale_job_skipped(self, temp_jobs_dir):
        """Job missed by >15 min is skipped (staleness guard)."""
        from job.runner import is_job_due
        from job.state import get_last_run, set_last_run

        # Last run was yesterday at 9:00
        last_run = datetime(2026, 2, 4, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # Now is today at 11:00 — the 09:00 schedule is 2 hours stale
        now = datetime(2026, 2, 5, 11, 0, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now) is False

        # State should be advanced to now
        assert get_last_run("test-job") == now

    def test_stale_boundary_just_under_grace(self, temp_jobs_dir):
        """Job at exactly grace_minutes - 1 min is still due."""
        from job.runner import is_job_due
        from job.state import set_last_run

        # Last run was yesterday at 9:00
        last_run = datetime(2026, 2, 4, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # Now is today at 9:14 — 14 min past schedule (under 15 min grace)
        now = datetime(2026, 2, 5, 9, 14, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now) is True

    def test_stale_boundary_just_over_grace(self, temp_jobs_dir):
        """Job at grace_minutes + 1 min is skipped."""
        from job.runner import is_job_due
        from job.state import set_last_run

        # Last run was yesterday at 9:00
        last_run = datetime(2026, 2, 4, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # Now is today at 9:16 — 16 min past schedule (over 15 min grace)
        now = datetime(2026, 2, 5, 9, 16, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now) is False

    def test_every_minute_stale_after_long_outage(self, temp_jobs_dir):
        """Every-minute job skipped when 30 min stale (doesn't catch up 30 runs)."""
        from job.runner import is_job_due
        from job.state import get_last_run, set_last_run

        # Last run 30 min ago
        last_run = datetime(2026, 2, 5, 8, 30, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        now = datetime(2026, 2, 5, 9, 0, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "* * * * *", now) is False

        # State advanced to now, so next check at 9:01 will fire normally
        assert get_last_run("test-job") == now

    def test_custom_grace_minutes(self, temp_jobs_dir):
        """Custom grace_minutes override works."""
        from job.runner import is_job_due
        from job.state import set_last_run

        # Last run was yesterday at 9:00
        last_run = datetime(2026, 2, 4, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # 20 min past schedule — would be stale with default 15 min, but OK with 30 min grace
        now = datetime(2026, 2, 5, 9, 20, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now, grace_minutes=30) is True


class TestBuildPrompt:
    """The prompt a run hands its agent: the marker, a blank line, the job's
    prompt, nothing about delivery."""

    def test_job_marker(self):
        from job.runs import build_prompt

        text = build_prompt("daily", {"prompt": "Check the releases"}, "schedule")
        assert text == "[Job: daily]\n\nCheck the releases"

    def test_webhook_marker(self):
        from job.runs import build_prompt

        text = build_prompt("daily", {"prompt": "Triage"}, "webhook")
        assert text.startswith("[Triggered by webhook: daily]\n\n")

    def test_no_delivery_instructions(self):
        from job.runs import build_prompt

        text = build_prompt("daily", {"prompt": "Do the thing"}, "manual")
        assert "discord" not in text.lower()
        assert "send" not in text.lower()


class TestRunJob:
    """run_job: the lock, the launch, the record."""

    def _job(self, **over):
        job = {
            "description": "Test job",
            "schedule": "0 9 * * *",
            "prompt": "Do something",
            "enabled": True,
            "report_mode": "always",
        }
        job.update(over)
        return job

    def test_run_job_opens_a_window_for_the_job(self, temp_jobs_dir, fake_launch):
        from job.runner import run_job

        result = run_job("test-job", self._job())
        assert result is not None and result.exit_code == 0
        assert len(fake_launch.runs) == 1
        run = fake_launch.runs[0]
        assert run.job_id == "test-job"
        assert run.is_agent and run.agent == "claude"
        assert run.deadline == 21600
        assert (run.dir / "prompt").read_text() == "[Job: test-job]\n\nDo something"
        assert result.run_id == run.run_id
        assert result.window == run.target

    def test_run_job_uses_the_jobs_agent(self, temp_jobs_dir, fake_launch):
        from job.runner import run_job

        run_job("codex-job", self._job(agent="codex"))
        assert fake_launch.runs[0].agent == "codex"
        assert (
            "codex --dangerously-bypass-approvals-and-sandbox"
            in (fake_launch.runs[0].dir / "run.sh").read_text()
        )

    def test_command_job_has_the_command_deadline(self, temp_jobs_dir, fake_launch):
        from job.runner import run_job

        run_job("cmd", self._job(type="command", command="echo hi"))
        run = fake_launch.runs[0]
        assert not run.is_agent and run.agent is None
        assert run.deadline == 3600
        assert not (run.dir / "prompt").exists()

    def test_run_job_updates_state_and_history(self, temp_jobs_dir, fake_launch):
        """A scheduled run advances the schedule cursor and records history."""
        from job.runner import run_job
        from job.state import get_history, get_last_run

        fake_launch.output = "the output"
        run_job("test-job", self._job(), trigger="schedule")

        # A scheduled run advances the cursor
        assert get_last_run("test-job") is not None

        history = get_history("test-job")
        assert len(history) == 1
        assert history[0]["exit_code"] == 0
        assert history[0]["agent"] == "claude"
        assert history[0]["run_id"] == fake_launch.runs[0].run_id
        assert history[0]["trigger"] == "schedule"
        assert "cost_usd" not in history[0]
        # The run directory is marked recorded exactly once.
        assert (fake_launch.runs[0].dir / "result.json").exists()

    def test_run_log_carries_agent_and_run_id(
        self, temp_jobs_dir, fake_launch, monkeypatch
    ):
        from job import logs as job_logs
        from job.runner import run_job

        monkeypatch.setattr(job_logs, "_logs_base_dir", lambda: temp_jobs_dir / "rl")
        fake_launch.output = "pane text"
        run_job("test-job", self._job())
        entries = job_logs.list_logs("test-job")
        assert entries[0]["agent"] == "claude"
        assert entries[0]["run_id"] == fake_launch.runs[0].run_id
        assert "session_id" not in entries[0]
        full = job_logs.read_log("test-job", entries[0]["timestamp"])
        assert full["output"] == "pane text"

    def test_record_run_records_once(self, temp_jobs_dir, fake_launch):
        """A second record of the same run (the orphan path racing a
        supervisor) writes nothing."""
        from job import supervisor
        from job.runner import record_run, run_job
        from job.state import get_history

        run_job("test-job", self._job())
        run = fake_launch.runs[0]
        record_run(
            run, supervisor.Outcome(exit_code=0, finished=run.started, output="")
        )
        assert len(get_history("test-job")) == 1

    def test_non_scheduled_run_does_not_advance_cursor(
        self, temp_jobs_dir, fake_launch
    ):
        """A webhook or manual run records history but leaves the schedule
        cursor untouched, so it can't consume an upcoming scheduled slot (#7)."""
        from job.runner import run_job
        from job.state import get_history, get_last_run

        job = {"schedule": "0 9 * * *", "prompt": "x", "enabled": True}

        for trig in ("webhook", "manual"):
            run_job(f"j-{trig}", job, trigger=trig)
            # No cursor written...
            assert get_last_run(f"j-{trig}") is None
            # ...but the run is still recorded in history (drives the display).
            assert len(get_history(f"j-{trig}")) == 1

    def test_webhook_run_does_not_swallow_scheduled_slot(
        self, temp_jobs_dir, fake_launch
    ):
        """The concrete #7 scenario: the schedule cursor sits at the last
        scheduled fire; a webhook run in between must leave it there so the
        next scheduled slot still comes due (grace fires it)."""
        from datetime import datetime, timedelta, timezone

        from job.runner import is_job_due, run_job
        from job.state import get_last_run, set_last_run

        job = {"schedule": "0 9 * * *", "prompt": "x", "enabled": True}

        # Yesterday's scheduled fire is the cursor.
        yesterday_9 = datetime.now(tz=timezone.utc).replace(
            hour=9, minute=0, second=0, microsecond=0
        ) - timedelta(days=1)
        set_last_run("daily", yesterday_9)

        # A webhook fires and runs.
        run_job("daily", job, trigger="webhook")

        # Cursor unchanged: still yesterday's fire, not the webhook time.
        assert get_last_run("daily") == yesterday_9

        # A few minutes after today's 09:00 slot, the job is still due
        # (within grace): the slot was not swallowed.
        just_after_9 = datetime.now(tz=timezone.utc).replace(
            hour=9, minute=5, second=0, microsecond=0
        )
        assert is_job_due("daily", "0 9 * * *", just_after_9, tz=timezone.utc)

    def test_run_job_skips_if_locked(self, temp_jobs_dir, fake_launch):
        """run_job skips execution if job is already locked."""
        from job.runner import run_job
        from job.state import acquire_job_lock, release_job_lock

        lock = acquire_job_lock("test-job")
        assert lock is not None
        assert run_job("test-job", self._job()) is None
        assert fake_launch.runs == []
        release_job_lock(lock)

    def test_launch_that_cannot_happen_is_a_failed_run(
        self, temp_jobs_dir, fake_launch, monkeypatch
    ):
        """No window, a clear message in history and the run log, the agent's
        exit code (127 for a missing binary, 1 for missing hooks)."""
        from job import agents, logs as job_logs
        from job.runner import run_job
        from job.state import get_history

        monkeypatch.setattr(job_logs, "_logs_base_dir", lambda: temp_jobs_dir / "rl")
        monkeypatch.setattr(
            agents, "launch_problem", lambda agent: (127, "claude is not installed")
        )
        result = run_job("test-job", self._job())
        assert result is not None
        assert result.exit_code == 127
        assert result.window == ""
        assert fake_launch.runs == []
        assert get_history("test-job")[0]["exit_code"] == 127
        entry = job_logs.list_logs("test-job")[0]
        full = job_logs.read_log("test-job", entry["timestamp"])
        assert "claude is not installed" in full["output"]
        # No run directory is left behind.
        from job import runs

        assert runs.run_dirs() == []

    def test_window_that_cannot_open_is_a_failed_run(
        self, temp_jobs_dir, fake_launch, monkeypatch
    ):
        from job import runner, runs
        from job.runner import run_job

        def refuse(run):
            raise runs.LaunchError(1, "could not open a tmux window for the run.")

        monkeypatch.setattr(runner.runs, "launch", refuse)
        result = run_job("test-job", self._job())
        assert result is not None and result.exit_code == 1
        assert "tmux window" in result.result
        assert runs.run_dirs() == []


class TestRunDispatcher:
    """Tests for run_dispatcher function."""

    def test_dispatcher_runs_due_jobs(self, temp_jobs_dir, fake_launch):
        """Dispatcher runs jobs that are due."""
        from job.runner import run_dispatcher
        from job.state import set_last_run

        # Create a job that runs every minute
        create_job_file(temp_jobs_dir, "every-minute", schedule="* * * * *")
        # Set last run to 2 min ago so it's due but not stale
        set_last_run(
            "every-minute", datetime.now(tz=timezone.utc) - timedelta(minutes=2)
        )

        run_dispatcher()
        assert [r.job_id for r in fake_launch.runs] == ["every-minute"]
        assert fake_launch.runs[0].trigger == "schedule"

    def test_dispatcher_skips_disabled_jobs(self, temp_jobs_dir, fake_launch):
        """Dispatcher skips disabled jobs."""
        from job.runner import run_dispatcher

        create_job_file(
            temp_jobs_dir, "disabled-job", schedule="* * * * *", enabled=False
        )
        run_dispatcher()
        assert fake_launch.runs == []

    def test_dispatcher_skips_not_due_jobs(self, temp_jobs_dir, fake_launch):
        """Dispatcher skips jobs that aren't due yet."""
        from job.runner import run_dispatcher
        from job.state import set_last_run

        # Create job that runs at 9:00
        create_job_file(temp_jobs_dir, "daily-job", schedule="0 9 * * *")

        # Set last run to just now
        set_last_run("daily-job", datetime.now(tz=timezone.utc))

        run_dispatcher()
        assert fake_launch.runs == []

    def test_dispatcher_continues_after_job_error(self, temp_jobs_dir, fake_launch):
        """Dispatcher continues running other jobs if one fails."""
        from job.runner import run_dispatcher
        from job.state import set_last_run

        create_job_file(temp_jobs_dir, "job1", schedule="* * * * *")
        create_job_file(temp_jobs_dir, "job2", schedule="* * * * *")
        # Set last run to 2 min ago so they're due
        two_min_ago = datetime.now(tz=timezone.utc) - timedelta(minutes=2)
        set_last_run("job1", two_min_ago)
        set_last_run("job2", two_min_ago)

        fake_launch.fail = RuntimeError("Simulated error")
        run_dispatcher()  # Should not raise

        # Both jobs should have been attempted
        assert sorted(r.job_id for r in fake_launch.runs) == ["job1", "job2"]

    def test_dispatcher_runs_jobs_in_parallel(self, temp_jobs_dir, fake_launch):
        """Dispatcher runs multiple due jobs concurrently."""
        from job.runner import run_dispatcher
        from job.state import set_last_run

        # Create 3 jobs all due
        for i in range(3):
            create_job_file(temp_jobs_dir, f"job{i}", schedule="* * * * *")
            set_last_run(
                f"job{i}", datetime.now(tz=timezone.utc) - timedelta(minutes=2)
            )

        fake_launch.delay = 0.3
        start = time.time()
        run_dispatcher()
        wall_time = time.time() - start

        # All 3 should have run
        assert len(fake_launch.runs) == 3

        # Wall time should be ~0.3s (parallel), not ~0.9s (sequential)
        # Use generous margin for CI environments
        assert wall_time < 0.8, f"Expected parallel execution but took {wall_time:.2f}s"

    def test_dispatcher_skips_stale_jobs_on_restart(self, temp_jobs_dir, fake_launch):
        """After a restart, jobs >15 min past schedule are skipped."""
        from job.runner import run_dispatcher
        from job.state import set_last_run

        # Simulate: last ran yesterday at 02:00, now it's 04:00 (2 hours stale)
        yesterday = datetime(2026, 2, 4, 2, 0, 0, tzinfo=timezone.utc)
        for job_id in ["job-a", "job-b"]:
            create_job_file(temp_jobs_dir, job_id, schedule="0 2 * * *")
            set_last_run(job_id, yesterday)

        with patch(
            "job.runner._now",
            return_value=datetime(2026, 2, 5, 4, 0, 0, tzinfo=timezone.utc),
        ):
            run_dispatcher()
        assert fake_launch.runs == []  # Both jobs are stale

    def test_dispatcher_never_seen_jobs_not_run(self, temp_jobs_dir, fake_launch):
        """Brand new jobs (no state) are registered but not executed."""
        from job.runner import run_dispatcher
        from job.state import get_last_run

        create_job_file(temp_jobs_dir, "brand-new", schedule="0 9 * * *")
        # No set_last_run: this is a never-seen job

        run_dispatcher()
        assert fake_launch.runs == []

        # But state should be initialized
        assert get_last_run("brand-new") is not None

    def test_dispatcher_runs_retention_tick(
        self, temp_jobs_dir, fake_launch, monkeypatch
    ):
        """Every tick finalizes orphans and closes windows past retention."""
        from job import runner

        calls = []
        monkeypatch.setattr(
            runner.retention,
            "tick",
            lambda **kw: calls.append(1) or {"finalized": 0, "closed": 0, "removed": 0},
        )
        runner.run_dispatcher()
        assert calls == [1]


# ---------------------------------------------------------------------------
# Working directory resolution (shared chain, both job types)
# ---------------------------------------------------------------------------


class TestResolveWorkingDir:
    def test_job_working_dir_wins(self, monkeypatch):
        from job.runner import resolve_working_dir

        monkeypatch.setenv("MERLIN_LAUNCH_CWD", "/launch")
        assert resolve_working_dir({"working_dir": "/explicit"}) == "/explicit"

    def test_falls_back_to_launch_cwd(self, monkeypatch):
        from job.runner import resolve_working_dir

        monkeypatch.setenv("MERLIN_LAUNCH_CWD", "/launch")
        assert resolve_working_dir({}) == "/launch"

    def test_falls_back_to_home(self, monkeypatch):
        from pathlib import Path

        from job.runner import resolve_working_dir

        monkeypatch.delenv("MERLIN_LAUNCH_CWD", raising=False)
        assert resolve_working_dir({}) == str(Path.home())

    def test_empty_working_dir_treated_as_unset(self, monkeypatch):
        from job.runner import resolve_working_dir

        monkeypatch.setenv("MERLIN_LAUNCH_CWD", "/launch")
        assert resolve_working_dir({"working_dir": ""}) == "/launch"


class TestAgentJobCwd:
    def test_agent_job_passes_explicit_working_dir(
        self, temp_jobs_dir, fake_launch, tmp_path
    ):
        from job.runner import run_job

        run_job("my-job", {"prompt": "do it", "working_dir": str(tmp_path)})
        assert fake_launch.runs[0].cwd == str(tmp_path)

    def test_agent_job_defaults_to_launch_cwd(
        self, temp_jobs_dir, fake_launch, monkeypatch, tmp_path
    ):
        from job.runner import run_job

        monkeypatch.setenv("MERLIN_LAUNCH_CWD", str(tmp_path))
        run_job("my-job", {"prompt": "do it"})
        assert fake_launch.runs[0].cwd == str(tmp_path)

    def test_agent_job_defaults_to_home_without_env(
        self, temp_jobs_dir, fake_launch, monkeypatch
    ):
        from pathlib import Path

        from job.runner import run_job

        monkeypatch.delenv("MERLIN_LAUNCH_CWD", raising=False)
        run_job("my-job", {"prompt": "do it"})
        assert fake_launch.runs[0].cwd == str(Path.home())


class TestEmitResult:
    """The job_complete stdout line is a subprocess contract, suppressible."""

    def _job(self):
        return {"type": "command", "command": "echo hi", "schedule": "* * * * *"}

    def test_default_emits_job_complete(self, capsys, temp_jobs_dir, fake_launch):
        from job.runner import run_job

        result = run_job("emit-test", self._job())
        assert result is not None and result.exit_code == 0
        out = capsys.readouterr().out
        assert '"type": "job_complete"' in out
        assert "cost_usd" not in out and "session_id" not in out

    def test_emit_result_false_keeps_stdout_clean(
        self, capsys, temp_jobs_dir, fake_launch
    ):
        from job.runner import run_job

        result = run_job("quiet-test", self._job(), emit_result=False)
        assert result is not None and result.exit_code == 0
        assert '"job_complete"' not in capsys.readouterr().out

    def test_locked_job_returns_none(self, monkeypatch):
        import job.runner as runner

        monkeypatch.setattr(runner, "acquire_job_lock", lambda job_id: None)
        assert runner.run_job("locked-test", self._job()) is None
