"""Tests for cron_runner.py — cron job dispatcher."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# croniter is needed for testing - install with: uv pip install croniter
pytest.importorskip("croniter")


@pytest.fixture
def temp_cron_dir(tmp_path):
    """Fixture that patches cron_runner and cron_state to use a temporary directory."""
    import cron_runner
    import cron_state

    # Save originals
    orig = {
        "runner_dir": cron_runner.CRON_JOBS_DIR,
        "state_dir": cron_state.STATE_DIR,
        "locks_dir": cron_state.LOCKS_DIR,
        "history_file": cron_state.HISTORY_FILE,
    }

    # Patch all paths
    cron_runner.CRON_JOBS_DIR = tmp_path
    cron_state.STATE_DIR = tmp_path / ".state"
    cron_state.LOCKS_DIR = tmp_path / ".locks"
    cron_state.HISTORY_FILE = tmp_path / ".history.json"

    yield tmp_path

    # Restore
    cron_runner.CRON_JOBS_DIR = orig["runner_dir"]
    cron_state.STATE_DIR = orig["state_dir"]
    cron_state.LOCKS_DIR = orig["locks_dir"]
    cron_state.HISTORY_FILE = orig["history_file"]


def create_job_file(directory: Path, job_id: str, **overrides) -> Path:
    """Helper to create a job file with defaults."""
    job = {
        "description": f"Test job {job_id}",
        "schedule": "0 9 * * *",
        "prompt": f"Test prompt for {job_id}",
        "channel": "123456789",
        "enabled": True,
        "report_mode": "always",
        "max_turns": 20,
        "created_at": "2026-02-05T00:00:00Z",
    }
    job.update(overrides)

    path = directory / f"{job_id}.json"
    path.write_text(json.dumps(job))
    return path


def make_mock_result(**kwargs):
    """Create a mock invoke_claude result."""
    defaults = {
        "exit_code": 0,
        "duration": 1.0,
        "session_id": "test-session",
        "stderr": "",
        "cost_usd": 0.05,
    }
    defaults.update(kwargs)
    result = MagicMock()
    for k, v in defaults.items():
        setattr(result, k, v)
    return result


class TestLoadJob:
    """Tests for load_job function."""

    def test_load_valid_job(self, temp_cron_dir):
        """load_job returns job data for valid JSON."""
        from cron_runner import load_job

        path = create_job_file(temp_cron_dir, "test-job")
        job = load_job(path)
        assert job is not None
        assert job["description"] == "Test job test-job"
        assert job["schedule"] == "0 9 * * *"

    def test_load_job_invalid_json(self, temp_cron_dir):
        """load_job returns None for invalid JSON."""
        from cron_runner import load_job

        path = temp_cron_dir / "bad.json"
        path.write_text("not valid json")
        assert load_job(path) is None

    def test_load_job_missing_required_field(self, temp_cron_dir):
        """load_job returns None when required fields are missing."""
        from cron_runner import load_job

        path = temp_cron_dir / "missing.json"
        path.write_text(json.dumps({"description": "test"}))  # missing schedule, prompt, channel
        assert load_job(path) is None

    def test_load_job_invalid_cron_expression(self, temp_cron_dir):
        """load_job returns None for invalid cron expression."""
        from cron_runner import load_job

        path = create_job_file(temp_cron_dir, "bad-cron", schedule="invalid cron")
        assert load_job(path) is None


class TestLoadAllJobs:
    """Tests for load_all_jobs function."""

    def test_load_all_jobs_empty(self, temp_cron_dir):
        """load_all_jobs returns empty dict when no jobs exist."""
        from cron_runner import load_all_jobs

        assert load_all_jobs() == {}

    def test_load_all_jobs_multiple(self, temp_cron_dir):
        """load_all_jobs loads all valid job files."""
        from cron_runner import load_all_jobs

        create_job_file(temp_cron_dir, "job1")
        create_job_file(temp_cron_dir, "job2")

        jobs = load_all_jobs()
        assert len(jobs) == 2
        assert "job1" in jobs
        assert "job2" in jobs

    def test_load_all_jobs_skips_dotfiles(self, temp_cron_dir):
        """load_all_jobs skips files starting with dot."""
        from cron_runner import load_all_jobs

        create_job_file(temp_cron_dir, "job1")
        (temp_cron_dir / ".state.json").write_text("{}")
        (temp_cron_dir / ".history.json").write_text("{}")

        jobs = load_all_jobs()
        assert len(jobs) == 1
        assert "job1" in jobs

    def test_load_all_jobs_skips_templates(self, temp_cron_dir):
        """load_all_jobs skips files starting with underscore."""
        from cron_runner import load_all_jobs

        create_job_file(temp_cron_dir, "job1")
        (temp_cron_dir / "_example.json.template").write_text("{}")

        jobs = load_all_jobs()
        assert len(jobs) == 1
        assert "job1" in jobs

    def test_load_all_jobs_skips_invalid(self, temp_cron_dir):
        """load_all_jobs skips invalid job files gracefully."""
        from cron_runner import load_all_jobs

        create_job_file(temp_cron_dir, "valid-job")
        (temp_cron_dir / "invalid.json").write_text("not json")

        jobs = load_all_jobs()
        assert len(jobs) == 1
        assert "valid-job" in jobs


class TestIsJobDue:
    """Tests for is_job_due function."""

    def test_never_seen_not_due(self, temp_cron_dir):
        """Never-seen job is NOT due (guard initializes state)."""
        from cron_runner import is_job_due

        now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
        assert is_job_due("new-job", "0 9 * * *", now) is False

    def test_never_seen_initializes_state(self, temp_cron_dir):
        """Never-seen job gets its state initialized to now."""
        from cron_runner import is_job_due
        from cron_state import get_last_run

        now = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
        is_job_due("new-job", "0 9 * * *", now)

        last_run = get_last_run("new-job")
        assert last_run == now

    def test_job_due_after_schedule(self, temp_cron_dir):
        """Job is due when scheduled time has passed since last run."""
        from cron_runner import is_job_due
        from cron_state import set_last_run

        # Last run was yesterday at 9:00
        last_run = datetime(2026, 2, 4, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # Now is today at 10:00 (after 9:00 schedule, within 15 min grace)
        now = datetime(2026, 2, 5, 9, 5, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now) is True

    def test_job_not_due_before_schedule(self, temp_cron_dir):
        """Job is not due when scheduled time hasn't passed."""
        from cron_runner import is_job_due
        from cron_state import set_last_run

        # Last run was today at 9:00
        last_run = datetime(2026, 2, 5, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # Now is today at 9:30 (before next 9:00)
        now = datetime(2026, 2, 5, 9, 30, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now) is False

    def test_job_not_due_immediately_after_run(self, temp_cron_dir):
        """Job is not due immediately after it was run."""
        from cron_runner import is_job_due
        from cron_state import set_last_run

        now = datetime(2026, 2, 5, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", now)

        # Even with "every minute" schedule, not due at same minute
        assert is_job_due("test-job", "* * * * *", now) is False

    def test_stale_job_skipped(self, temp_cron_dir):
        """Job missed by >15 min is skipped (staleness guard)."""
        from cron_runner import is_job_due
        from cron_state import get_last_run, set_last_run

        # Last run was yesterday at 9:00
        last_run = datetime(2026, 2, 4, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # Now is today at 11:00 — the 09:00 schedule is 2 hours stale
        now = datetime(2026, 2, 5, 11, 0, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now) is False

        # State should be advanced to now
        assert get_last_run("test-job") == now

    def test_stale_boundary_just_under_grace(self, temp_cron_dir):
        """Job at exactly grace_minutes - 1 min is still due."""
        from cron_runner import is_job_due
        from cron_state import set_last_run

        # Last run was yesterday at 9:00
        last_run = datetime(2026, 2, 4, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # Now is today at 9:14 — 14 min past schedule (under 15 min grace)
        now = datetime(2026, 2, 5, 9, 14, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now) is True

    def test_stale_boundary_just_over_grace(self, temp_cron_dir):
        """Job at grace_minutes + 1 min is skipped."""
        from cron_runner import is_job_due
        from cron_state import set_last_run

        # Last run was yesterday at 9:00
        last_run = datetime(2026, 2, 4, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # Now is today at 9:16 — 16 min past schedule (over 15 min grace)
        now = datetime(2026, 2, 5, 9, 16, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now) is False

    def test_every_minute_stale_after_long_outage(self, temp_cron_dir):
        """Every-minute job skipped when 30 min stale (doesn't catch up 30 runs)."""
        from cron_runner import is_job_due
        from cron_state import get_last_run, set_last_run

        # Last run 30 min ago
        last_run = datetime(2026, 2, 5, 8, 30, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        now = datetime(2026, 2, 5, 9, 0, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "* * * * *", now) is False

        # State advanced to now, so next check at 9:01 will fire normally
        assert get_last_run("test-job") == now

    def test_custom_grace_minutes(self, temp_cron_dir):
        """Custom grace_minutes override works."""
        from cron_runner import is_job_due
        from cron_state import set_last_run

        # Last run was yesterday at 9:00
        last_run = datetime(2026, 2, 4, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", last_run)

        # 20 min past schedule — would be stale with default 15 min, but OK with 30 min grace
        now = datetime(2026, 2, 5, 9, 20, 0, tzinfo=timezone.utc)
        assert is_job_due("test-job", "0 9 * * *", now, grace_minutes=30) is True


class TestBuildPrompt:
    """Tests for build_prompt function."""

    def test_build_prompt_silent_mode(self):
        """build_prompt appends silent instruction for report_mode: silent."""
        from cron_runner import build_prompt

        job = {"prompt": "Search for X", "report_mode": "silent"}
        prompt = build_prompt(job)
        assert "Search for X" in prompt
        assert "Only send a message to Discord if you have something noteworthy" in prompt

    def test_build_prompt_always_mode(self):
        """build_prompt appends always instruction for report_mode: always."""
        from cron_runner import build_prompt

        job = {"prompt": "Search for X", "report_mode": "always"}
        prompt = build_prompt(job)
        assert "Search for X" in prompt
        assert "Send your findings to Discord even if there's nothing new" in prompt

    def test_build_prompt_default_mode(self):
        """build_prompt defaults to always mode when not specified."""
        from cron_runner import build_prompt

        job = {"prompt": "Search for X"}  # no report_mode
        prompt = build_prompt(job)
        assert "Send your findings to Discord even if there's nothing new" in prompt


class TestSessionId:
    """Tests for session_id_for_job function."""

    def test_session_id_deterministic(self):
        """session_id_for_job returns same ID for same job."""
        from cron_runner import session_id_for_job

        s1 = session_id_for_job("test-job")
        s2 = session_id_for_job("test-job")
        assert s1 == s2

    def test_session_id_different_jobs(self):
        """session_id_for_job returns different IDs for different jobs."""
        from cron_runner import session_id_for_job

        s1 = session_id_for_job("job1")
        s2 = session_id_for_job("job2")
        assert s1 != s2

    def test_session_id_is_valid_uuid(self):
        """session_id_for_job returns a valid UUID string."""
        import uuid

        from cron_runner import session_id_for_job

        s = session_id_for_job("test-job")
        # Should not raise
        uuid.UUID(s)


class TestRunJob:
    """Tests for run_job function."""

    def test_run_job_calls_invoke_claude(self, temp_cron_dir):
        """run_job calls invoke_claude with correct arguments."""
        from cron_runner import run_job, session_id_for_job

        job = {
            "description": "Test job",
            "schedule": "0 9 * * *",
            "prompt": "Do something",
            "channel": "123456789",
            "enabled": True,
            "report_mode": "always",
            "max_turns": 10,
        }

        with patch("cron_runner.invoke_claude", return_value=make_mock_result()) as mock_invoke:
            run_job("test-job", job)

            mock_invoke.assert_called_once()
            call_args = mock_invoke.call_args
            assert "Do something" in call_args[0][0]  # prompt
            assert call_args[1]["caller"] == "cron-test-job"
            assert call_args[1]["max_turns"] == 10

    def test_run_job_updates_state_and_history(self, temp_cron_dir):
        """run_job updates state and history after execution."""
        from cron_runner import run_job
        from cron_state import get_history, get_last_run

        job = {
            "description": "Test job",
            "schedule": "0 9 * * *",
            "prompt": "Do something",
            "channel": "123456789",
            "enabled": True,
            "report_mode": "always",
        }

        with patch("cron_runner.invoke_claude", return_value=make_mock_result(duration=2.5, cost_usd=0.10)):
            run_job("test-job", job)

        # State should be updated
        assert get_last_run("test-job") is not None

        # History should have entry
        history = get_history("test-job")
        assert len(history) == 1
        assert history[0]["exit_code"] == 0
        assert history[0]["duration"] == 2.5
        assert history[0]["cost_usd"] == 0.10

    def test_run_job_retries_on_session_not_found(self, temp_cron_dir):
        """run_job retries with --session-id when session not found (non-ephemeral only)."""
        from cron_runner import run_job

        job = {
            "description": "Test job",
            "schedule": "0 9 * * *",
            "prompt": "Do something",
            "channel": "123456789",
            "enabled": True,
            "report_mode": "always",
            "ephemeral": False,
        }

        fail_result = make_mock_result(exit_code=1, stderr="No conversation found for session")
        success_result = make_mock_result()

        with patch("cron_runner.invoke_claude", side_effect=[fail_result, success_result]) as mock:
            run_job("test-job", job)

            assert mock.call_count == 2
            # First call with resume=True
            assert mock.call_args_list[0][1]["resume"] is True
            # Second call with resume=False
            assert mock.call_args_list[1][1]["resume"] is False

    def test_run_job_skips_if_locked(self, temp_cron_dir):
        """run_job skips execution if job is already locked."""
        from cron_runner import run_job
        from cron_state import acquire_job_lock, release_job_lock

        job = {
            "description": "Test job",
            "schedule": "0 9 * * *",
            "prompt": "Do something",
            "channel": "123456789",
            "enabled": True,
            "report_mode": "always",
        }

        # Hold the lock
        lock = acquire_job_lock("test-job")
        assert lock is not None

        with patch("cron_runner.invoke_claude") as mock:
            run_job("test-job", job)
            mock.assert_not_called()  # Should be skipped

        release_job_lock(lock)


class TestRunDispatcher:
    """Tests for run_dispatcher function."""

    def test_dispatcher_runs_due_jobs(self, temp_cron_dir):
        """Dispatcher runs jobs that are due."""
        from cron_runner import run_dispatcher
        from cron_state import set_last_run

        # Create a job that runs every minute
        create_job_file(temp_cron_dir, "every-minute", schedule="* * * * *")
        # Set last run to 2 min ago so it's due but not stale
        set_last_run("every-minute", datetime.now(tz=timezone.utc) - timedelta(minutes=2))

        with patch("cron_runner.invoke_claude", return_value=make_mock_result()) as mock:
            run_dispatcher()
            assert mock.call_count == 1

    def test_dispatcher_skips_disabled_jobs(self, temp_cron_dir):
        """Dispatcher skips disabled jobs."""
        from cron_runner import run_dispatcher

        create_job_file(temp_cron_dir, "disabled-job", schedule="* * * * *", enabled=False)

        with patch("cron_runner.invoke_claude") as mock:
            run_dispatcher()
            mock.assert_not_called()

    def test_dispatcher_skips_not_due_jobs(self, temp_cron_dir):
        """Dispatcher skips jobs that aren't due yet."""
        from cron_runner import run_dispatcher
        from cron_state import set_last_run

        # Create job that runs at 9:00
        create_job_file(temp_cron_dir, "daily-job", schedule="0 9 * * *")

        # Set last run to just now
        set_last_run("daily-job", datetime.now(tz=timezone.utc))

        with patch("cron_runner.invoke_claude") as mock:
            run_dispatcher()
            mock.assert_not_called()

    def test_dispatcher_continues_after_job_error(self, temp_cron_dir):
        """Dispatcher continues running other jobs if one fails."""
        from cron_runner import run_dispatcher
        from cron_state import set_last_run

        create_job_file(temp_cron_dir, "job1", schedule="* * * * *")
        create_job_file(temp_cron_dir, "job2", schedule="* * * * *")
        # Set last run to 2 min ago so they're due
        two_min_ago = datetime.now(tz=timezone.utc) - timedelta(minutes=2)
        set_last_run("job1", two_min_ago)
        set_last_run("job2", two_min_ago)

        call_count = 0

        def mock_invoke(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Simulated error")
            return make_mock_result()

        with patch("cron_runner.invoke_claude", side_effect=mock_invoke):
            run_dispatcher()  # Should not raise

        # Both jobs should have been attempted
        assert call_count == 2

    def test_dispatcher_runs_jobs_in_parallel(self, temp_cron_dir):
        """Dispatcher runs multiple due jobs concurrently."""
        from cron_runner import run_dispatcher
        from cron_state import set_last_run

        # Create 3 jobs all due
        for i in range(3):
            create_job_file(temp_cron_dir, f"job{i}", schedule="* * * * *")
            set_last_run(f"job{i}", datetime.now(tz=timezone.utc) - timedelta(minutes=2))

        execution_times = {}

        def slow_invoke(*args, **kwargs):
            job_caller = kwargs.get("caller", "")
            execution_times[job_caller] = {"start": time.time()}
            time.sleep(0.3)  # Simulate work
            execution_times[job_caller]["end"] = time.time()
            return make_mock_result()

        with patch("cron_runner.invoke_claude", side_effect=slow_invoke):
            start = time.time()
            run_dispatcher()
            wall_time = time.time() - start

        # All 3 should have run
        assert len(execution_times) == 3

        # Wall time should be ~0.3s (parallel), not ~0.9s (sequential)
        # Use generous margin for CI environments
        assert wall_time < 0.8, f"Expected parallel execution but took {wall_time:.2f}s"

    def test_dispatcher_skips_stale_jobs_on_restart(self, temp_cron_dir):
        """After a restart, jobs >15 min past schedule are skipped."""
        from cron_runner import run_dispatcher
        from cron_state import set_last_run

        # Simulate: last ran yesterday at 02:00, now it's 04:00 (2 hours stale)
        yesterday = datetime(2026, 2, 4, 2, 0, 0, tzinfo=timezone.utc)
        for job_id in ["job-a", "job-b"]:
            create_job_file(temp_cron_dir, job_id, schedule="0 2 * * *")
            set_last_run(job_id, yesterday)

        with patch("cron_runner._now", return_value=datetime(2026, 2, 5, 4, 0, 0, tzinfo=timezone.utc)):
            with patch("cron_runner.invoke_claude") as mock:
                run_dispatcher()
                mock.assert_not_called()  # Both jobs are stale

    def test_dispatcher_never_seen_jobs_not_run(self, temp_cron_dir):
        """Brand new jobs (no state) are registered but not executed."""
        from cron_runner import run_dispatcher
        from cron_state import get_last_run

        create_job_file(temp_cron_dir, "brand-new", schedule="0 9 * * *")
        # No set_last_run — this is a never-seen job

        with patch("cron_runner.invoke_claude") as mock:
            run_dispatcher()
            mock.assert_not_called()

        # But state should be initialized
        assert get_last_run("brand-new") is not None

