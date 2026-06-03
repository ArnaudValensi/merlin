"""Tests for per-job timezone resolution + DST-aware due checks in cron.runner."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("croniter")


@pytest.fixture
def temp_cron_dir(tmp_path):
    from cron import runner as cron_runner
    from cron import state as cron_state

    orig = {
        "runner_dir": cron_runner.CRON_JOBS_DIR,
        "state_dir": cron_state.STATE_DIR,
        "locks_dir": cron_state.LOCKS_DIR,
        "history_file": cron_state.HISTORY_FILE,
        "cron_tz": cron_runner.CRON_TZ,
    }
    cron_runner.CRON_JOBS_DIR = tmp_path
    cron_state.STATE_DIR = tmp_path / ".state"
    cron_state.LOCKS_DIR = tmp_path / ".locks"
    cron_state.HISTORY_FILE = tmp_path / ".history.json"
    yield tmp_path
    cron_runner.CRON_JOBS_DIR = orig["runner_dir"]
    cron_state.STATE_DIR = orig["state_dir"]
    cron_state.LOCKS_DIR = orig["locks_dir"]
    cron_state.HISTORY_FILE = orig["history_file"]
    cron_runner.CRON_TZ = orig["cron_tz"]


class TestJobTimezone:
    def test_per_job_timezone_wins(self):
        from cron.runner import job_timezone

        assert job_timezone({"timezone": "Europe/Paris"}) == ZoneInfo("Europe/Paris")

    def test_invalid_falls_back_to_cron_tz(self, monkeypatch):
        from cron import runner as cron_runner

        monkeypatch.setattr(cron_runner, "CRON_TZ", ZoneInfo("America/New_York"))
        assert cron_runner.job_timezone({"timezone": "Bogus/Zone"}) == ZoneInfo(
            "America/New_York"
        )

    def test_no_timezone_uses_cron_tz(self, monkeypatch):
        from cron import runner as cron_runner

        monkeypatch.setattr(cron_runner, "CRON_TZ", None)
        assert cron_runner.job_timezone({}) is None


class TestIsJobDueWithTz:
    def test_schedule_interpreted_in_job_tz(self, temp_cron_dir):
        """ "0 17 * * *" in Europe/Paris is due at 17:00 Paris, not 17:00 UTC."""
        from cron.runner import is_job_due
        from cron.state import set_last_run

        paris = ZoneInfo("Europe/Paris")
        # Last run yesterday 17:00 Paris.
        set_last_run("p", datetime(2026, 1, 14, 17, 0, tzinfo=paris))

        # 17:00 Paris on the 15th == 16:00 UTC (winter, UTC+1).
        now_utc = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)
        assert is_job_due("p", "0 17 * * *", now_utc, tz=paris) is True

    def test_not_due_before_local_time(self, temp_cron_dir):
        from cron.runner import is_job_due
        from cron.state import set_last_run

        paris = ZoneInfo("Europe/Paris")
        set_last_run("p", datetime(2026, 1, 14, 17, 0, tzinfo=paris))
        # 15:00 UTC == 16:00 Paris winter — before the 17:00 schedule.
        now_utc = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
        assert is_job_due("p", "0 17 * * *", now_utc, tz=paris) is False
