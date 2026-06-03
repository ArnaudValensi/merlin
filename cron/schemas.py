"""Pydantic models for cron job REST API."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

# Job ID pattern: lowercase + alphanumeric + single hyphens, no --, max 30 chars, starts with letter
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")

VALID_REPORT_MODES = ("always", "silent", "off")


def _validate_timezone(v: str | None) -> str | None:
    """Validate an optional IANA timezone name. None/empty is allowed (means
    'use the server default')."""
    if v is None or v == "":
        return v
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(v)
    except Exception:
        raise ValueError(f"Invalid timezone: {v}") from None
    return v


class JobCreate(BaseModel):
    id: str
    description: str = ""
    schedule: str
    timezone: str | None = None
    type: Literal["prompt", "command"] = "prompt"
    prompt: str = ""
    command: str = ""
    working_dir: str | None = None
    enabled: bool = True
    report_mode: str = "always"
    max_turns: int = 0
    ephemeral: bool = True
    grace_minutes: int = 15
    discord_channel: str | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if len(v) > 30:
            raise ValueError("id must be at most 30 characters")
        if not _ID_RE.match(v):
            raise ValueError(
                "id must start with a letter, contain only lowercase letters, "
                "digits, and single hyphens (no '--')"
            )
        return v

    @field_validator("schedule")
    @classmethod
    def validate_schedule(cls, v: str) -> str:
        from croniter import croniter

        if not croniter.is_valid(v):
            raise ValueError(f"Invalid cron expression: {v}")
        return v

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str | None) -> str | None:
        return _validate_timezone(v)

    @field_validator("report_mode")
    @classmethod
    def validate_report_mode(cls, v: str) -> str:
        if v not in VALID_REPORT_MODES:
            raise ValueError(f"report_mode must be one of {VALID_REPORT_MODES}")
        return v

    @model_validator(mode="after")
    def validate_action(self) -> "JobCreate":
        """Require the action field that matches the job type."""
        if self.type == "command":
            if not self.command.strip():
                raise ValueError("command must be non-empty for a command job")
        else:  # "prompt"
            if not self.prompt.strip():
                raise ValueError("prompt must be non-empty for a prompt job")
        return self


class JobUpdate(BaseModel):
    description: str | None = None
    schedule: str | None = None
    timezone: str | None = None
    type: Literal["prompt", "command"] | None = None
    prompt: str | None = None
    command: str | None = None
    working_dir: str | None = None
    enabled: bool | None = None
    report_mode: str | None = None
    max_turns: int | None = None
    ephemeral: bool | None = None
    grace_minutes: int | None = None
    discord_channel: str | None = None

    @field_validator("schedule")
    @classmethod
    def validate_schedule(cls, v: str | None) -> str | None:
        if v is not None:
            from croniter import croniter

            if not croniter.is_valid(v):
                raise ValueError(f"Invalid cron expression: {v}")
        return v

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str | None) -> str | None:
        return _validate_timezone(v)

    @field_validator("report_mode")
    @classmethod
    def validate_report_mode(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_REPORT_MODES:
            raise ValueError(f"report_mode must be one of {VALID_REPORT_MODES}")
        return v

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("prompt must be non-empty")
        return v
