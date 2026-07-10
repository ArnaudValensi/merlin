"""Tests for job Pydantic schemas — type-aware action validation."""

import pytest

pytest.importorskip("croniter")

from pydantic import ValidationError

from job.schemas import JobCreate, JobUpdate


class TestJobCreateType:
    def test_default_type_is_prompt(self):
        job = JobCreate(id="j", schedule="0 9 * * *", prompt="do something")
        assert job.type == "prompt"
        assert job.command == ""
        assert job.working_dir is None

    def test_prompt_job_requires_prompt(self):
        with pytest.raises(ValidationError):
            JobCreate(id="j", schedule="0 9 * * *", type="prompt")

    def test_prompt_job_empty_prompt_raises(self):
        with pytest.raises(ValidationError):
            JobCreate(id="j", schedule="0 9 * * *", type="prompt", prompt="   ")

    def test_command_job_requires_command(self):
        with pytest.raises(ValidationError):
            JobCreate(id="j", schedule="0 9 * * *", type="command")

    def test_command_job_empty_command_raises(self):
        with pytest.raises(ValidationError):
            JobCreate(id="j", schedule="0 9 * * *", type="command", command="  ")

    def test_command_job_allows_empty_prompt(self):
        job = JobCreate(
            id="j",
            schedule="0 9 * * *",
            type="command",
            command="echo hi",
        )
        assert job.type == "command"
        assert job.command == "echo hi"
        assert job.prompt == ""

    def test_command_job_persists_working_dir(self):
        job = JobCreate(
            id="j",
            schedule="0 9 * * *",
            type="command",
            command="echo hi",
            working_dir="/tmp/foo",
        )
        assert job.working_dir == "/tmp/foo"

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            JobCreate(id="j", schedule="0 9 * * *", type="webhook", prompt="x")


class TestReportMode:
    def test_off_accepted(self):
        job = JobCreate(id="j", schedule="0 9 * * *", prompt="x", report_mode="off")
        assert job.report_mode == "off"

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValidationError):
            JobCreate(id="j", schedule="0 9 * * *", prompt="x", report_mode="verbose")

    def test_update_accepts_off(self):
        assert JobUpdate(report_mode="off").report_mode == "off"


class TestJobTimezone:
    def test_default_timezone_is_none(self):
        job = JobCreate(id="j", schedule="0 9 * * *", prompt="x")
        assert job.timezone is None

    def test_valid_timezone_accepted(self):
        job = JobCreate(
            id="j", schedule="0 9 * * *", prompt="x", timezone="Europe/Paris"
        )
        assert job.timezone == "Europe/Paris"

    def test_invalid_timezone_rejected(self):
        with pytest.raises(ValidationError):
            JobCreate(id="j", schedule="0 9 * * *", prompt="x", timezone="Not/AZone")

    def test_update_accepts_timezone(self):
        body = JobUpdate(timezone="America/New_York")
        assert body.timezone == "America/New_York"

    def test_update_invalid_timezone_rejected(self):
        with pytest.raises(ValidationError):
            JobUpdate(timezone="Bogus/Zone")


class TestOptionalTriggers:
    def test_job_without_schedule_is_valid(self):
        job = JobCreate(id="j", prompt="x")
        assert job.schedule is None
        assert job.webhook is None

    def test_empty_schedule_normalizes_to_none(self):
        job = JobCreate(id="j", schedule="", prompt="x")
        assert job.schedule is None

    def test_invalid_schedule_still_rejected(self):
        with pytest.raises(ValidationError):
            JobCreate(id="j", schedule="not a cron", prompt="x")

    def test_webhook_block_accepted(self):
        job = JobCreate(id="j", prompt="x", webhook={"secret": "whk_abc"})
        assert job.webhook is not None
        assert job.webhook.secret == "whk_abc"

    def test_webhook_requires_secret(self):
        with pytest.raises(ValidationError):
            JobCreate(id="j", prompt="x", webhook={})

    def test_webhook_empty_secret_rejected(self):
        with pytest.raises(ValidationError):
            JobCreate(id="j", prompt="x", webhook={"secret": "   "})

    def test_schedule_and_webhook_together(self):
        job = JobCreate(
            id="j",
            schedule="0 9 * * *",
            prompt="x",
            webhook={"secret": "whk_abc"},
        )
        assert job.schedule == "0 9 * * *"
        assert job.webhook.secret == "whk_abc"

    def test_update_empty_schedule_allowed_for_removal(self):
        """'' means "remove the schedule trigger"; the route pops the key."""
        body = JobUpdate(schedule="")
        assert body.schedule == ""

    def test_update_invalid_schedule_rejected(self):
        with pytest.raises(ValidationError):
            JobUpdate(schedule="nope")


class TestJobUpdatePartial:
    def test_partial_command_update_validates(self):
        body = JobUpdate(type="command", command="echo hi", working_dir="/tmp")
        assert body.type == "command"
        assert body.command == "echo hi"
        assert body.working_dir == "/tmp"

    def test_partial_update_no_cross_field_rule(self):
        """A partial update may set type=command without supplying a command."""
        body = JobUpdate(type="command")
        assert body.type == "command"
        assert body.command is None

    def test_explicit_empty_prompt_rejected(self):
        with pytest.raises(ValidationError):
            JobUpdate(prompt="")

    def test_none_prompt_allowed(self):
        body = JobUpdate(description="x")
        assert body.prompt is None
