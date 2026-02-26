"""Tests for cron_manage.py — cron job management script."""

import json
from pathlib import Path

import pytest

pytest.importorskip("croniter")


@pytest.fixture
def temp_cron_dir(tmp_path):
    """Fixture that patches cron_manage to use a temporary directory."""
    import cron_manage
    import cron_state

    orig = {
        "manage_dir": cron_manage.CRON_JOBS_DIR,
        "state_dir": cron_state.STATE_DIR,
        "locks_dir": cron_state.LOCKS_DIR,
        "history_file": cron_state.HISTORY_FILE,
    }

    cron_manage.CRON_JOBS_DIR = tmp_path
    cron_state.STATE_DIR = tmp_path / ".state"
    cron_state.LOCKS_DIR = tmp_path / ".locks"
    cron_state.HISTORY_FILE = tmp_path / ".history.json"

    yield tmp_path

    cron_manage.CRON_JOBS_DIR = orig["manage_dir"]
    cron_state.STATE_DIR = orig["state_dir"]
    cron_state.LOCKS_DIR = orig["locks_dir"]
    cron_state.HISTORY_FILE = orig["history_file"]


class TestValidateCron:
    """Tests for cron expression validation."""

    def test_valid_expressions(self):
        from cron_manage import validate_cron

        assert validate_cron("* * * * *") is True
        assert validate_cron("0 9 * * *") is True
        assert validate_cron("0 9 * * 1-5") is True
        assert validate_cron("*/5 * * * *") is True

    def test_invalid_expressions(self):
        from cron_manage import validate_cron

        assert validate_cron("invalid") is False
        assert validate_cron("* * *") is False
        assert validate_cron("60 * * * *") is False


class TestCronToHuman:
    """Tests for cron to human-readable conversion."""

    def test_common_patterns(self):
        from cron_manage import cron_to_human

        assert cron_to_human("* * * * *") == "every minute"
        assert cron_to_human("0 * * * *") == "every hour"
        assert cron_to_human("0 9 * * *") == "daily at 9:00"
        assert cron_to_human("0 9 * * 1") == "Mondays at 9:00"
        assert cron_to_human("0 8 * * 1-5") == "weekdays at 8:00"

    def test_interval_patterns(self):
        from cron_manage import cron_to_human

        assert cron_to_human("0 */2 * * *") == "every 2 hours"
        assert cron_to_human("*/15 * * * *") == "every 15 minutes"


class TestSlugify:
    """Tests for slugify function."""

    def test_basic_slugify(self):
        from cron_manage import slugify

        assert slugify("Daily Weather Check") == "daily-weather-check"
        assert slugify("My Test Job!") == "my-test-job"
        assert slugify("  spaces  everywhere  ") == "spaces-everywhere"

    def test_special_characters(self):
        from cron_manage import slugify

        assert slugify("test@job#123") == "testjob123"
        assert slugify("über-cool") == "ber-cool"

    def test_truncation(self):
        from cron_manage import slugify

        long_text = "a" * 100
        assert len(slugify(long_text)) <= 50


class TestCmdAdd:
    """Tests for add command."""

    def test_add_job_success(self, temp_cron_dir):
        from cron_manage import cmd_add
        from types import SimpleNamespace

        args = SimpleNamespace(
            id="test-job",
            schedule="0 9 * * *",
            prompt="Test prompt",
            channel="123",
            description="Test job",
            report_mode="always",
            max_turns=20,
            dry_run=False,
        )

        result = cmd_add(args)
        assert result["ok"] is True
        assert result["job_id"] == "test-job"
        assert (temp_cron_dir / "test-job.json").exists()

    def test_add_job_dry_run(self, temp_cron_dir):
        from cron_manage import cmd_add
        from types import SimpleNamespace

        args = SimpleNamespace(
            id="test-job",
            schedule="0 9 * * *",
            prompt="Test prompt",
            channel="123",
            description="Test job",
            report_mode="always",
            max_turns=20,
            dry_run=True,
        )

        result = cmd_add(args)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert not (temp_cron_dir / "test-job.json").exists()

    def test_add_job_invalid_cron(self, temp_cron_dir):
        from cron_manage import cmd_add
        from types import SimpleNamespace

        args = SimpleNamespace(
            id="test-job",
            schedule="invalid",
            prompt="Test prompt",
            channel="123",
            description="Test job",
            report_mode="always",
            max_turns=20,
            dry_run=False,
        )

        result = cmd_add(args)
        assert result["ok"] is False
        assert "Invalid cron expression" in result["error"]

    def test_add_job_duplicate(self, temp_cron_dir):
        from cron_manage import cmd_add, save_job
        from types import SimpleNamespace

        # Create existing job
        save_job("test-job", {"schedule": "0 9 * * *", "prompt": "x", "channel": "1"})

        args = SimpleNamespace(
            id="test-job",
            schedule="0 9 * * *",
            prompt="Test prompt",
            channel="123",
            description="Test job",
            report_mode="always",
            max_turns=20,
            dry_run=False,
        )

        result = cmd_add(args)
        assert result["ok"] is False
        assert "already exists" in result["error"]

    def test_add_job_auto_id(self, temp_cron_dir):
        from cron_manage import cmd_add
        from types import SimpleNamespace

        args = SimpleNamespace(
            id=None,
            schedule="0 9 * * *",
            prompt="Test prompt",
            channel="123",
            description="My Daily Check",
            report_mode="always",
            max_turns=20,
            dry_run=False,
        )

        result = cmd_add(args)
        assert result["ok"] is True
        assert result["job_id"] == "my-daily-check"


class TestCmdList:
    """Tests for list command."""

    def test_list_empty(self, temp_cron_dir):
        from cron_manage import cmd_list
        from types import SimpleNamespace

        args = SimpleNamespace(discord=False)
        result = cmd_list(args)
        assert result["ok"] is True
        assert result["count"] == 0

    def test_list_with_jobs(self, temp_cron_dir):
        from cron_manage import cmd_list, save_job
        from types import SimpleNamespace

        save_job("job1", {"schedule": "0 9 * * *", "prompt": "x", "channel": "1", "description": "Job 1"})
        save_job("job2", {"schedule": "0 10 * * *", "prompt": "y", "channel": "2", "description": "Job 2"})

        args = SimpleNamespace(discord=False)
        result = cmd_list(args)
        assert result["ok"] is True
        assert result["count"] == 2

    def test_list_discord_format(self, temp_cron_dir):
        from cron_manage import cmd_list, save_job
        from types import SimpleNamespace

        save_job("test-job", {
            "schedule": "0 9 * * *",
            "prompt": "x",
            "channel": "1",
            "description": "Test",
            "enabled": True,
            "report_mode": "silent",
        })

        args = SimpleNamespace(discord=True)
        result = cmd_list(args)
        assert isinstance(result, str)
        assert "**Cron jobs (1 active)**" in result
        assert "✅" in result
        assert "**test-job**" in result
        assert "daily at 9:00" in result
        assert "silent" in result


class TestCmdEnableDisable:
    """Tests for enable/disable commands."""

    def test_disable_job(self, temp_cron_dir):
        from cron_manage import cmd_disable, load_job, save_job
        from types import SimpleNamespace

        save_job("test-job", {"schedule": "0 9 * * *", "prompt": "x", "channel": "1", "enabled": True})

        args = SimpleNamespace(job_id="test-job")
        result = cmd_disable(args)
        assert result["ok"] is True

        job = load_job("test-job")
        assert job["enabled"] is False

    def test_enable_job(self, temp_cron_dir):
        from cron_manage import cmd_enable, load_job, save_job
        from types import SimpleNamespace

        save_job("test-job", {"schedule": "0 9 * * *", "prompt": "x", "channel": "1", "enabled": False})

        args = SimpleNamespace(job_id="test-job")
        result = cmd_enable(args)
        assert result["ok"] is True

        job = load_job("test-job")
        assert job["enabled"] is True

    def test_disable_nonexistent(self, temp_cron_dir):
        from cron_manage import cmd_disable
        from types import SimpleNamespace

        args = SimpleNamespace(job_id="nonexistent")
        result = cmd_disable(args)
        assert result["ok"] is False
        assert "not found" in result["error"]


class TestCmdRemove:
    """Tests for remove command."""

    def test_remove_job(self, temp_cron_dir):
        from cron_manage import cmd_remove, save_job
        from types import SimpleNamespace

        save_job("test-job", {"schedule": "0 9 * * *", "prompt": "x", "channel": "1"})

        args = SimpleNamespace(job_id="test-job")
        result = cmd_remove(args)
        assert result["ok"] is True
        assert not (temp_cron_dir / "test-job.json").exists()

    def test_remove_nonexistent(self, temp_cron_dir):
        from cron_manage import cmd_remove
        from types import SimpleNamespace

        args = SimpleNamespace(job_id="nonexistent")
        result = cmd_remove(args)
        assert result["ok"] is False


class TestCmdHistory:
    """Tests for history command."""

    def test_history_empty(self, temp_cron_dir):
        from cron_manage import cmd_history
        from types import SimpleNamespace

        args = SimpleNamespace(job_id="test-job", limit=None, discord=False)
        result = cmd_history(args)
        assert result["ok"] is True
        assert result["runs"] == []

    def test_history_with_runs(self, temp_cron_dir):
        from cron_manage import cmd_history
        from cron_state import append_history
        from types import SimpleNamespace

        append_history("test-job", exit_code=0, duration=1.5)
        append_history("test-job", exit_code=1, duration=2.0)

        args = SimpleNamespace(job_id="test-job", limit=None, discord=False)
        result = cmd_history(args)
        assert result["ok"] is True
        assert len(result["runs"]) == 2

    def test_history_discord_format(self, temp_cron_dir):
        from cron_manage import cmd_history
        from cron_state import append_history
        from types import SimpleNamespace

        append_history("test-job", exit_code=0, duration=1.5)

        args = SimpleNamespace(job_id="test-job", limit=None, discord=True)
        result = cmd_history(args)
        assert isinstance(result, str)
        assert "**Recent runs: test-job**" in result
        assert "✅" in result


class TestFormatting:
    """Tests for Discord formatting functions."""

    def test_format_jobs_empty(self):
        from cron_manage import format_jobs_discord

        result = format_jobs_discord([])
        assert result == "**No cron jobs configured.**"

    def test_format_jobs_with_disabled(self):
        from cron_manage import format_jobs_discord

        jobs = [
            {"id": "job1", "schedule": "0 9 * * *", "enabled": True, "report_mode": "silent"},
            {"id": "job2", "schedule": "0 10 * * *", "enabled": False, "report_mode": "always"},
        ]
        result = format_jobs_discord(jobs)
        assert "1 active, 1 disabled" in result
        assert "✅" in result
        assert "⏸️" in result
