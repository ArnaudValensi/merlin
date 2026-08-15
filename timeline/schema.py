"""Versioned, provider-neutral activity event schema."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = 1
MAX_NAME_BYTES = 160
MAX_ATTRIBUTES_BYTES = 2048
MAX_CONTEXT_BYTES = 2048
MAX_ATTRIBUTE_DEPTH = 3
MAX_ATTRIBUTE_ITEMS = 32

_ID_RE = re.compile(r"^[A-Za-z0-9%@][A-Za-z0-9._:/%@-]{0,127}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+$")
_ATTR_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_BLOCKED_ATTRIBUTE_KEYS = {
    "command",
    "content",
    "error",
    "input",
    "message",
    "output",
    "prompt",
    "response",
    "result",
    "secret",
    "stderr",
    "stdout",
    "tool_input",
    "tool_output",
    "tool_response",
}

ActivityPhase = Literal["point", "start", "finish"]
ActivityStatus = Literal[
    "running",
    "ok",
    "error",
    "blocked",
    "timeout",
    "interrupted",
    "unknown",
]
ActorType = Literal["human", "agent", "automation"]


def _safe_text(value: str, *, field_name: str, max_bytes: int) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field_name} must be one line without control characters")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field_name} exceeds {max_bytes} UTF-8 bytes")
    return value


def _safe_id(value: str, *, field_name: str) -> str:
    value = value.strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} is not a safe identifier")
    return value


def _validate_attribute_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_ATTRIBUTE_DEPTH:
        raise ValueError("attributes are nested too deeply")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _safe_text(value, field_name="attribute value", max_bytes=512)
    if isinstance(value, list):
        if len(value) > MAX_ATTRIBUTE_ITEMS:
            raise ValueError("attribute list has too many items")
        return [_validate_attribute_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_ATTRIBUTE_ITEMS:
            raise ValueError("attribute object has too many items")
        checked: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _ATTR_KEY_RE.fullmatch(key):
                raise ValueError("attribute key is not safe")
            if key.lower() in _BLOCKED_ATTRIBUTE_KEYS:
                raise ValueError(f"attribute key {key!r} is content-bearing")
            checked[key] = _validate_attribute_value(item, depth=depth + 1)
        return checked
    raise ValueError("attribute values must be JSON scalars, lists, or objects")


class ActivityActor(BaseModel):
    """The participant responsible for an event."""

    model_config = ConfigDict(extra="allow")

    type: ActorType
    id: str
    label: str
    role: str | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _safe_id(value, field_name="actor.id")

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        return _safe_text(value, field_name="actor.label", max_bytes=96)

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _safe_text(value, field_name="actor.role", max_bytes=48)

    @model_validator(mode="after")
    def validate_extra_fields(self):
        for key, value in (self.model_extra or {}).items():
            if not _ATTR_KEY_RE.fullmatch(key):
                raise ValueError("actor key is not safe")
            if key.lower() in _BLOCKED_ATTRIBUTE_KEYS:
                raise ValueError(f"actor key {key!r} is content-bearing")
            _validate_attribute_value(value)
        return self


class ActivityContext(BaseModel):
    """Truthful optional metadata available at the event source."""

    model_config = ConfigDict(extra="allow")

    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    project: str | None = None
    cwd: str | None = None
    tmux_session: str | None = None
    tmux_window: str | None = None
    tmux_pane: str | None = None
    agent_sid: str | None = None
    session_file: str | None = None
    artifact_path: str | None = None

    @field_validator(
        "provider", "model", "effort", "project", "tmux_session", "tmux_window"
    )
    @classmethod
    def validate_short_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _safe_text(value, field_name=f"context.{info.field_name}", max_bytes=128)

    @field_validator("cwd", "session_file", "artifact_path")
    @classmethod
    def validate_path_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _safe_text(value, field_name=f"context.{info.field_name}", max_bytes=512)

    @field_validator("tmux_pane", "agent_sid")
    @classmethod
    def validate_context_id(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _safe_id(value, field_name=f"context.{info.field_name}")

    @model_validator(mode="after")
    def validate_extra_fields(self):
        for key, value in (self.model_extra or {}).items():
            if not _ATTR_KEY_RE.fullmatch(key):
                raise ValueError("context key is not safe")
            if key.lower() in _BLOCKED_ATTRIBUTE_KEYS:
                raise ValueError(f"context key {key!r} is content-bearing")
            _validate_attribute_value(value)
        encoded = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        if len(encoded) > MAX_CONTEXT_BYTES:
            raise ValueError(f"context exceeds {MAX_CONTEXT_BYTES} UTF-8 bytes")
        return self


class ActivityEvent(BaseModel):
    """One v1 point, span start, or span finish record."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: str
    timestamp: datetime
    phase: ActivityPhase
    kind: str
    trace_id: str
    span_id: str | None = None
    parent_span_id: str | None = None
    actor: ActivityActor
    context: ActivityContext = Field(default_factory=ActivityContext)
    status: ActivityStatus
    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("event_id must be a UUID") from exc
        return str(parsed)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a UTC offset")
        return value.astimezone(timezone.utc)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        value = value.strip().lower()
        if not _KIND_RE.fullmatch(value):
            raise ValueError("kind must be a lowercase dotted name")
        return value

    @field_validator("trace_id", "span_id", "parent_span_id")
    @classmethod
    def validate_ids(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _safe_id(value, field_name=info.field_name)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _safe_text(value, field_name="name", max_bytes=MAX_NAME_BYTES)

    @field_validator("attributes")
    @classmethod
    def validate_attributes(cls, value: dict[str, Any]) -> dict[str, Any]:
        checked = _validate_attribute_value(value)
        encoded = json.dumps(
            checked, separators=(",", ":"), ensure_ascii=False
        ).encode()
        if len(encoded) > MAX_ATTRIBUTES_BYTES:
            raise ValueError(f"attributes exceed {MAX_ATTRIBUTES_BYTES} UTF-8 bytes")
        return checked

    @model_validator(mode="after")
    def validate_phase_contract(self):
        for key, value in (self.model_extra or {}).items():
            if not _ATTR_KEY_RE.fullmatch(key):
                raise ValueError("event key is not safe")
            if key.lower() in _BLOCKED_ATTRIBUTE_KEYS:
                raise ValueError(f"event key {key!r} is content-bearing")
            _validate_attribute_value(value)
        if self.phase == "point" and self.span_id is not None:
            raise ValueError("point events must not have span_id")
        if self.phase != "point" and self.span_id is None:
            raise ValueError("start and finish events require span_id")
        if self.phase == "start" and self.status not in {"running", "blocked"}:
            raise ValueError("start status must be running or blocked")
        if self.phase == "finish" and self.status in {"running", "blocked"}:
            raise ValueError("finish status must be terminal")
        return self


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
