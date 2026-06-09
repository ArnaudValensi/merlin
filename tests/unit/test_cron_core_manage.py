"""Tests for cron.manage — cron job management script."""

import pytest

pytest.importorskip("croniter")


@pytest.fixture
def temp_cron_dir(tmp_path):
    """Fixture that patches cron.manage to use a temporary directory."""
    from cron import manage as cron_manage
    from cron import state as cron_state

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
        from cron.manage import validate_cron

        assert validate_cron("* * * * *") is True
        assert validate_cron("0 9 * * *") is True
        assert validate_cron("0 9 * * 1-5") is True
        assert validate_cron("*/5 * * * *") is True

    def test_invalid_expressions(self):
        from cron.manage import validate_cron

        assert validate_cron("invalid") is False
        assert validate_cron("* * *") is False
        assert validate_cron("60 * * * *") is False


class TestCronToHuman:
    """Tests for cron to human-readable conversion (via cron-descriptor)."""

    def test_common_patterns(self):
        from cron.manage import cron_to_human

        assert cron_to_human("* * * * *") == "every minute"
        assert cron_to_human("0 * * * *") == "every hour"
        assert cron_to_human("0 9 * * *") == "at 09:00"
        # Weekday restriction is described and not lowercased mid-string.
        assert "Monday" in cron_to_human("0 9 * * 1")
        weekdays = cron_to_human("0 8 * * 1-5")
        assert "08:00" in weekdays
        assert "Monday through Friday" in weekdays

    def test_interval_patterns(self):
        from cron.manage import cron_to_human

        assert cron_to_human("0 */2 * * *") == "every 2 hours"
        assert cron_to_human("*/15 * * * *") == "every 15 minutes"

    def test_monthly_pattern(self):
        from cron.manage import cron_to_human

        assert cron_to_human("0 9 1 * *") == "at 09:00, on day 1 of the month"

    def test_invalid_expression_falls_back_to_raw(self):
        from cron.manage import cron_to_human

        assert cron_to_human("not a cron") == "not a cron"
        assert cron_to_human("* * *") == "* * *"


class TestSlugify:
    """Tests for slugify function."""

    def test_basic_slugify(self):
        from cron.manage import slugify

        assert slugify("Daily Weather Check") == "daily-weather-check"
        assert slugify("My Test Job!") == "my-test-job"
        assert slugify("  spaces  everywhere  ") == "spaces-everywhere"

    def test_special_characters(self):
        from cron.manage import slugify

        assert slugify("test@job#123") == "testjob123"
        assert slugify("über-cool") == "ber-cool"

    def test_truncation(self):
        from cron.manage import slugify

        long_text = "a" * 100
        assert len(slugify(long_text)) <= 50


class TestCmdAdd:
    """Tests for add command."""

    def test_add_job_success(self, temp_cron_dir):
        from cron.manage import cmd_add
        from types import SimpleNamespace

        args = SimpleNamespace(
            id="test-job",
            schedule="0 9 * * *",
            prompt="Test prompt",
            discord_channel="123",
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
        from cron.manage import cmd_add
        from types import SimpleNamespace

        args = SimpleNamespace(
            id="test-job",
            schedule="0 9 * * *",
            prompt="Test prompt",
            discord_channel="123",
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
        from cron.manage import cmd_add
        from types import SimpleNamespace

        args = SimpleNamespace(
            id="test-job",
            schedule="invalid",
            prompt="Test prompt",
            discord_channel="123",
            description="Test job",
            report_mode="always",
            max_turns=20,
            dry_run=False,
        )

        result = cmd_add(args)
        assert result["ok"] is False
        assert "Invalid cron expression" in result["error"]

    def test_add_job_duplicate(self, temp_cron_dir):
        from cron.manage import cmd_add, save_job
        from types import SimpleNamespace

        # Create existing job
        save_job("test-job", {"schedule": "0 9 * * *", "prompt": "x", "channel": "1"})

        args = SimpleNamespace(
            id="test-job",
            schedule="0 9 * * *",
            prompt="Test prompt",
            discord_channel="123",
            description="Test job",
            report_mode="always",
            max_turns=20,
            dry_run=False,
        )

        result = cmd_add(args)
        assert result["ok"] is False
        assert "already exists" in result["error"]

    def test_add_job_auto_id(self, temp_cron_dir):
        from cron.manage import cmd_add
        from types import SimpleNamespace

        args = SimpleNamespace(
            id=None,
            schedule="0 9 * * *",
            prompt="Test prompt",
            discord_channel="123",
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
        from cron.manage import cmd_list
        from types import SimpleNamespace

        args = SimpleNamespace(discord=False)
        result = cmd_list(args)
        assert result["ok"] is True
        assert result["count"] == 0

    def test_list_with_jobs(self, temp_cron_dir):
        from cron.manage import cmd_list, save_job
        from types import SimpleNamespace

        save_job(
            "job1",
            {
                "schedule": "0 9 * * *",
                "prompt": "x",
                "channel": "1",
                "description": "Job 1",
            },
        )
        save_job(
            "job2",
            {
                "schedule": "0 10 * * *",
                "prompt": "y",
                "channel": "2",
                "description": "Job 2",
            },
        )

        args = SimpleNamespace(discord=False)
        result = cmd_list(args)
        assert result["ok"] is True
        assert result["count"] == 2

    def test_list_discord_format(self, temp_cron_dir):
        from cron.manage import cmd_list, save_job
        from types import SimpleNamespace

        save_job(
            "test-job",
            {
                "schedule": "0 9 * * *",
                "prompt": "x",
                "channel": "1",
                "description": "Test",
                "enabled": True,
                "report_mode": "silent",
            },
        )

        args = SimpleNamespace(discord=True)
        result = cmd_list(args)
        assert isinstance(result, str)
        assert "**Cron jobs (1 active)**" in result
        assert "**test-job**" in result
        assert "at 09:00" in result
        assert "silent" in result


class TestCmdEnableDisable:
    """Tests for enable/disable commands."""

    def test_disable_job(self, temp_cron_dir):
        from cron.manage import cmd_disable, load_job, save_job
        from types import SimpleNamespace

        save_job(
            "test-job",
            {"schedule": "0 9 * * *", "prompt": "x", "channel": "1", "enabled": True},
        )

        args = SimpleNamespace(job_id="test-job")
        result = cmd_disable(args)
        assert result["ok"] is True

        job = load_job("test-job")
        assert job["enabled"] is False

    def test_enable_job(self, temp_cron_dir):
        from cron.manage import cmd_enable, load_job, save_job
        from types import SimpleNamespace

        save_job(
            "test-job",
            {"schedule": "0 9 * * *", "prompt": "x", "channel": "1", "enabled": False},
        )

        args = SimpleNamespace(job_id="test-job")
        result = cmd_enable(args)
        assert result["ok"] is True

        job = load_job("test-job")
        assert job["enabled"] is True

    def test_disable_nonexistent(self, temp_cron_dir):
        from cron.manage import cmd_disable
        from types import SimpleNamespace

        args = SimpleNamespace(job_id="nonexistent")
        result = cmd_disable(args)
        assert result["ok"] is False
        assert "not found" in result["error"]


class TestCmdRemove:
    """Tests for remove command."""

    def test_remove_job(self, temp_cron_dir):
        from cron.manage import cmd_remove, save_job
        from types import SimpleNamespace

        save_job("test-job", {"schedule": "0 9 * * *", "prompt": "x", "channel": "1"})

        args = SimpleNamespace(job_id="test-job")
        result = cmd_remove(args)
        assert result["ok"] is True
        assert not (temp_cron_dir / "test-job.json").exists()

    def test_remove_nonexistent(self, temp_cron_dir):
        from cron.manage import cmd_remove
        from types import SimpleNamespace

        args = SimpleNamespace(job_id="nonexistent")
        result = cmd_remove(args)
        assert result["ok"] is False


class TestCmdHistory:
    """Tests for history command."""

    def test_history_empty(self, temp_cron_dir):
        from cron.manage import cmd_history
        from types import SimpleNamespace

        args = SimpleNamespace(job_id="test-job", limit=None, discord=False)
        result = cmd_history(args)
        assert result["ok"] is True
        assert result["runs"] == []

    def test_history_with_runs(self, temp_cron_dir):
        from cron.manage import cmd_history
        from cron.state import append_history
        from types import SimpleNamespace

        append_history("test-job", exit_code=0, duration=1.5)
        append_history("test-job", exit_code=1, duration=2.0)

        args = SimpleNamespace(job_id="test-job", limit=None, discord=False)
        result = cmd_history(args)
        assert result["ok"] is True
        assert len(result["runs"]) == 2

    def test_history_discord_format(self, temp_cron_dir):
        from cron.manage import cmd_history
        from cron.state import append_history
        from types import SimpleNamespace

        append_history("test-job", exit_code=0, duration=1.5)

        args = SimpleNamespace(job_id="test-job", limit=None, discord=True)
        result = cmd_history(args)
        assert isinstance(result, str)
        assert "**Recent runs: test-job**" in result


class TestFormatting:
    """Tests for Discord formatting functions."""

    def test_format_jobs_empty(self):
        from cron.manage import format_jobs_discord

        result = format_jobs_discord([])
        assert result == "**No cron jobs configured.**"

    def test_format_jobs_with_disabled(self):
        from cron.manage import format_jobs_discord

        jobs = [
            {
                "id": "job1",
                "schedule": "0 9 * * *",
                "enabled": True,
                "report_mode": "silent",
            },
            {
                "id": "job2",
                "schedule": "0 10 * * *",
                "enabled": False,
                "report_mode": "always",
            },
        ]
        result = format_jobs_discord(jobs)
        assert "1 active, 1 disabled" in result


class TestTrigger:
    """Tests for cmd_trigger — manual job execution via merlin cron trigger."""

    def test_trigger_unknown_job(self, temp_cron_dir, monkeypatch):
        from types import SimpleNamespace

        from cron import runner
        from cron.manage import cmd_trigger

        monkeypatch.setattr(runner, "load_all_jobs", lambda: {})
        result = cmd_trigger(SimpleNamespace(job_id="nope"))
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    @staticmethod
    def _fake_result(exit_code=0, duration=1.234, session_id="sess-1"):
        from types import SimpleNamespace

        return SimpleNamespace(
            exit_code=exit_code,
            duration=duration,
            result="output",
            stderr="",
            cost_usd=None,
            session_id=session_id,
        )

    def test_trigger_runs_job(self, temp_cron_dir, monkeypatch):
        from types import SimpleNamespace

        from cron import runner
        from cron.manage import cmd_trigger

        ran: list[str] = []

        def fake_run(job_id, *, emit_result=True):
            ran.append((job_id, emit_result))
            return self._fake_result()

        monkeypatch.setattr(
            runner, "load_all_jobs", lambda: {"my-job": {"schedule": "* * * * *"}}
        )
        monkeypatch.setattr(runner, "run_single_job", fake_run)

        result = cmd_trigger(SimpleNamespace(job_id="my-job"))
        assert result["ok"] is True
        assert result["exit_code"] == 0
        assert result["duration_seconds"] == 1.23
        assert result["session_id"] == "sess-1"
        # In-process trigger must suppress the runner's stdout job_complete line
        assert ran == [("my-job", False)]

    def test_trigger_reports_failed_job(self, temp_cron_dir, monkeypatch):
        """A job that ran but exited non-zero is reported as ok: false."""
        from types import SimpleNamespace

        from cron import runner
        from cron.manage import cmd_trigger

        monkeypatch.setattr(
            runner, "load_all_jobs", lambda: {"my-job": {"schedule": "* * * * *"}}
        )
        monkeypatch.setattr(
            runner,
            "run_single_job",
            lambda job_id, *, emit_result=True: self._fake_result(exit_code=1),
        )

        result = cmd_trigger(SimpleNamespace(job_id="my-job"))
        assert result["ok"] is False
        assert result["exit_code"] == 1
        assert "failed (exit 1)" in result["message"]

    def test_trigger_locked_job(self, temp_cron_dir, monkeypatch):
        from types import SimpleNamespace

        from cron import runner
        from cron.manage import cmd_trigger

        monkeypatch.setattr(
            runner, "load_all_jobs", lambda: {"my-job": {"schedule": "* * * * *"}}
        )
        monkeypatch.setattr(
            runner, "run_single_job", lambda job_id, *, emit_result=True: None
        )

        result = cmd_trigger(SimpleNamespace(job_id="my-job"))
        assert result["ok"] is False
        assert "already running" in result["error"]

    def test_trigger_failure_reported(self, temp_cron_dir, monkeypatch):
        from types import SimpleNamespace

        from cron import runner
        from cron.manage import cmd_trigger

        def boom(job_id, *, emit_result=True):
            raise SystemExit(1)

        monkeypatch.setattr(
            runner, "load_all_jobs", lambda: {"my-job": {"schedule": "* * * * *"}}
        )
        monkeypatch.setattr(runner, "run_single_job", boom)

        result = cmd_trigger(SimpleNamespace(job_id="my-job"))
        assert result["ok"] is False


class TestMainArgv:
    """main() accepts an explicit argv and prog (merlin cron delegation)."""

    def test_main_list_with_argv(self, temp_cron_dir, capsys):
        from cron.manage import main

        main(["list"])
        out = capsys.readouterr().out
        assert '"ok": true' in out

    def test_main_help_uses_prog(self, temp_cron_dir, capsys):
        from cron.manage import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--help"], prog="merlin cron")
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "merlin cron" in out

    def test_main_no_subcommand_prints_help(self, temp_cron_dir, capsys):
        from cron.manage import main

        # Bare `merlin cron`: print help and return, not an argparse error.
        result = main([])
        assert result is None
        out = capsys.readouterr().out
        assert "usage:" in out.lower()
