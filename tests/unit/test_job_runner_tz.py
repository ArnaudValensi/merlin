"""Tests for per-job timezone resolution + DST-aware due checks in job.runner."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("croniter")


@pytest.fixture
def temp_jobs_dir(tmp_path):
    from job import runner as job_runner
    from job import state as job_state

    orig = {
        "runner_dir": job_runner.JOBS_DIR,
        "state_dir": job_state.STATE_DIR,
        "locks_dir": job_state.LOCKS_DIR,
        "history_file": job_state.HISTORY_FILE,
        "job_tz": job_runner.CRON_TZ,
    }
    job_runner.JOBS_DIR = tmp_path
    job_state.STATE_DIR = tmp_path / ".state"
    job_state.LOCKS_DIR = tmp_path / ".locks"
    job_state.HISTORY_FILE = tmp_path / ".history.json"
    yield tmp_path
    job_runner.JOBS_DIR = orig["runner_dir"]
    job_state.STATE_DIR = orig["state_dir"]
    job_state.LOCKS_DIR = orig["locks_dir"]
    job_state.HISTORY_FILE = orig["history_file"]
    job_runner.CRON_TZ = orig["job_tz"]


class TestJobTimezone:
    def test_per_job_timezone_wins(self):
        from job.runner import job_timezone

        assert job_timezone({"timezone": "Europe/Paris"}) == ZoneInfo("Europe/Paris")

    def test_invalid_falls_back_to_cron_tz(self, monkeypatch):
        from job import runner as job_runner

        monkeypatch.setattr(job_runner, "CRON_TZ", ZoneInfo("America/New_York"))
        assert job_runner.job_timezone({"timezone": "Bogus/Zone"}) == ZoneInfo(
            "America/New_York"
        )

    def test_no_timezone_uses_cron_tz(self, monkeypatch):
        from job import runner as job_runner

        monkeypatch.setattr(job_runner, "CRON_TZ", None)
        assert job_runner.job_timezone({}) is None


class TestIsJobDueWithTz:
    def test_schedule_interpreted_in_job_tz(self, temp_jobs_dir):
        """ "0 17 * * *" in Europe/Paris is due at 17:00 Paris, not 17:00 UTC."""
        from job.runner import is_job_due
        from job.state import set_last_run

        paris = ZoneInfo("Europe/Paris")
        # Last run yesterday 17:00 Paris.
        set_last_run("p", datetime(2026, 1, 14, 17, 0, tzinfo=paris))

        # 17:00 Paris on the 15th == 16:00 UTC (winter, UTC+1).
        now_utc = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)
        assert is_job_due("p", "0 17 * * *", now_utc, tz=paris) is True

    def test_not_due_before_local_time(self, temp_jobs_dir):
        from job.runner import is_job_due
        from job.state import set_last_run

        paris = ZoneInfo("Europe/Paris")
        set_last_run("p", datetime(2026, 1, 14, 17, 0, tzinfo=paris))
        # 15:00 UTC == 16:00 Paris winter — before the 17:00 schedule.
        now_utc = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
        assert is_job_due("p", "0 17 * * *", now_utc, tz=paris) is False


class TestGraceZeroFootgun:
    def test_grace_zero_with_dispatcher_lag_still_due(self, temp_jobs_dir):
        """grace_minutes=0 must not mean 'never run': the dispatcher always
        fires a second or two after the minute, and sub-minute staleness is
        dispatch lag, not a missed run."""
        from job.runner import is_job_due
        from job.state import set_last_run

        set_last_run("g", datetime(2026, 6, 3, 10, 0, 2, tzinfo=timezone.utc))
        # Next slot 10:01:00; dispatcher evaluates at 10:01:02 (2s lag).
        now = datetime(2026, 6, 3, 10, 1, 2, tzinfo=timezone.utc)
        assert is_job_due("g", "* * * * *", now, grace_minutes=0) is True

    def test_grace_zero_still_skips_truly_missed_runs(self, temp_jobs_dir):
        from job.runner import is_job_due
        from job.state import set_last_run

        # Daily 10:00 job last ran yesterday; today's slot missed by 3 minutes.
        set_last_run("g", datetime(2026, 6, 2, 10, 0, 2, tzinfo=timezone.utc))
        now = datetime(2026, 6, 3, 10, 3, 2, tzinfo=timezone.utc)
        assert is_job_due("g", "0 10 * * *", now, grace_minutes=0) is False
