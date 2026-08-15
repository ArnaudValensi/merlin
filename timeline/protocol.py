"""Dependency-free validation for latency-sensitive activity producers."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 1
MAX_NAME_BYTES = 160
MAX_ATTRIBUTES_BYTES = 2048
MAX_CONTEXT_BYTES = 2048
MAX_ATTRIBUTE_DEPTH = 3
MAX_ATTRIBUTE_ITEMS = 32

ID_RE = re.compile(r"^[A-Za-z0-9%@][A-Za-z0-9._:/%@-]{0,127}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+$")
ATTR_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
BLOCKED_ATTRIBUTE_KEYS = {
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
PHASES = {"point", "start", "finish"}
STATUSES = {"running", "ok", "error", "blocked", "timeout", "interrupted", "unknown"}
ACTOR_TYPES = {"human", "agent", "automation"}
SHORT_CONTEXT_FIELDS = {
    "provider",
    "model",
    "effort",
    "project",
    "tmux_session",
    "tmux_window",
}
PATH_CONTEXT_FIELDS = {"cwd", "session_file", "artifact_path"}
ID_CONTEXT_FIELDS = {"tmux_pane", "agent_sid"}
RECORD_FIELDS = {
    "schema_version",
    "event_id",
    "timestamp",
    "phase",
    "kind",
    "trace_id",
    "span_id",
    "parent_span_id",
    "actor",
    "context",
    "status",
    "name",
    "attributes",
}
ACTOR_FIELDS = {"type", "id", "label", "role"}


class ProtocolError(ValueError):
    """A producer attempted to emit an invalid or content-bearing record."""


def safe_text(value: object, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{field} must be text")
    checked = value.strip()
    if not checked:
        raise ProtocolError(f"{field} must not be empty")
    if any(ord(char) < 32 or ord(char) == 127 for char in checked):
        raise ProtocolError(f"{field} must be one line without control characters")
    if len(checked.encode("utf-8")) > max_bytes:
        raise ProtocolError(f"{field} exceeds {max_bytes} UTF-8 bytes")
    return checked


def safe_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value.strip()):
        raise ProtocolError(f"{field} is not a safe identifier")
    return value.strip()


def _attribute_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_ATTRIBUTE_DEPTH:
        raise ProtocolError("attributes are nested too deeply")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return safe_text(value, field="attribute value", max_bytes=512)
    if isinstance(value, list):
        if len(value) > MAX_ATTRIBUTE_ITEMS:
            raise ProtocolError("attribute list has too many items")
        return [_attribute_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_ATTRIBUTE_ITEMS:
            raise ProtocolError("attribute object has too many items")
        checked = {}
        for key, item in value.items():
            if not isinstance(key, str) or not ATTR_KEY_RE.fullmatch(key):
                raise ProtocolError("attribute key is not safe")
            if key.lower() in BLOCKED_ATTRIBUTE_KEYS:
                raise ProtocolError(f"attribute key {key!r} is content-bearing")
            checked[key] = _attribute_value(item, depth=depth + 1)
        return checked
    raise ProtocolError("attribute values must be JSON scalars, lists, or objects")


def _context(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("context must be an object")
    if len(value) > MAX_ATTRIBUTE_ITEMS:
        raise ProtocolError("context has too many fields")
    checked: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not ATTR_KEY_RE.fullmatch(key):
            raise ProtocolError("context key is not safe")
        if key.lower() in BLOCKED_ATTRIBUTE_KEYS:
            raise ProtocolError(f"context key {key!r} is content-bearing")
        if item is None:
            checked[key] = None
        elif key in SHORT_CONTEXT_FIELDS:
            checked[key] = safe_text(item, field=f"context.{key}", max_bytes=128)
        elif key in PATH_CONTEXT_FIELDS:
            checked[key] = safe_text(item, field=f"context.{key}", max_bytes=512)
        elif key in ID_CONTEXT_FIELDS:
            checked[key] = safe_id(item, field=f"context.{key}")
        else:
            checked[key] = _attribute_value(item)
    encoded = json.dumps(checked, separators=(",", ":"), ensure_ascii=False).encode()
    if len(encoded) > MAX_CONTEXT_BYTES:
        raise ProtocolError(f"context exceeds {MAX_CONTEXT_BYTES} UTF-8 bytes")
    return checked


def validate_record(value: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one v1 record without importing the web stack."""
    if not isinstance(value, dict):
        raise ProtocolError("activity event must be an object")
    record = dict(value)
    for key in record.keys() - RECORD_FIELDS:
        if not isinstance(key, str) or not ATTR_KEY_RE.fullmatch(key):
            raise ProtocolError("event key is not safe")
        if key.lower() in BLOCKED_ATTRIBUTE_KEYS:
            raise ProtocolError(f"event key {key!r} is content-bearing")
        record[key] = _attribute_value(record[key])
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolError("schema_version must be 1")
    try:
        record["event_id"] = str(uuid.UUID(str(record.get("event_id"))))
    except ValueError as exc:
        raise ProtocolError("event_id must be a UUID") from exc

    timestamp = record.get("timestamp")
    if isinstance(timestamp, datetime):
        parsed = timestamp
    elif isinstance(timestamp, str):
        normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ProtocolError("timestamp must be ISO-8601") from exc
    else:
        raise ProtocolError("timestamp must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolError("timestamp must include a UTC offset")
    record["timestamp"] = (
        parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    phase = record.get("phase")
    if phase not in PHASES:
        raise ProtocolError("phase must be point, start, or finish")
    kind = safe_text(record.get("kind"), field="kind", max_bytes=128).lower()
    if not KIND_RE.fullmatch(kind):
        raise ProtocolError("kind must be a lowercase dotted name")
    record["kind"] = kind
    record["trace_id"] = safe_id(record.get("trace_id"), field="trace_id")

    span_id = record.get("span_id")
    if phase == "point" and span_id is not None:
        raise ProtocolError("point events must not have span_id")
    if phase != "point" and span_id is None:
        raise ProtocolError("start and finish events require span_id")
    if span_id is not None:
        record["span_id"] = safe_id(span_id, field="span_id")
    if record.get("parent_span_id") is not None:
        record["parent_span_id"] = safe_id(
            record["parent_span_id"], field="parent_span_id"
        )

    status = record.get("status")
    if status not in STATUSES:
        raise ProtocolError("status is invalid")
    if phase == "start" and status not in {"running", "blocked"}:
        raise ProtocolError("start status must be running or blocked")
    if phase == "finish" and status in {"running", "blocked"}:
        raise ProtocolError("finish status must be terminal")

    actor = record.get("actor")
    if not isinstance(actor, dict) or actor.get("type") not in ACTOR_TYPES:
        raise ProtocolError("actor.type is invalid")
    actor = dict(actor)
    for key in actor.keys() - ACTOR_FIELDS:
        if not isinstance(key, str) or not ATTR_KEY_RE.fullmatch(key):
            raise ProtocolError("actor key is not safe")
        if key.lower() in BLOCKED_ATTRIBUTE_KEYS:
            raise ProtocolError(f"actor key {key!r} is content-bearing")
        actor[key] = _attribute_value(actor[key])
    actor["id"] = safe_id(actor.get("id"), field="actor.id")
    actor["label"] = safe_text(actor.get("label"), field="actor.label", max_bytes=96)
    if actor.get("role") is not None:
        actor["role"] = safe_text(actor["role"], field="actor.role", max_bytes=48)
    record["actor"] = actor

    record["context"] = _context(record.get("context", {}))
    record["name"] = safe_text(
        record.get("name"), field="name", max_bytes=MAX_NAME_BYTES
    )
    attributes = record.get("attributes", {})
    if not isinstance(attributes, dict):
        raise ProtocolError("attributes must be an object")
    attributes = _attribute_value(attributes)
    encoded = json.dumps(attributes, separators=(",", ":"), ensure_ascii=False).encode()
    if len(encoded) > MAX_ATTRIBUTES_BYTES:
        raise ProtocolError(f"attributes exceed {MAX_ATTRIBUTES_BYTES} UTF-8 bytes")
    record["attributes"] = attributes
    return record
