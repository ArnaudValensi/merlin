"""Sanitize Claude Code and Codex hook payloads into the v1 event protocol."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .correlation import clear_pending, consume_once, remember_pending, take_pending
from .protocol import ID_RE, validate_record


_LABEL_RE = re.compile(r"[^A-Za-z0-9._/-]+")


def _namespace(provider: str, kind: str, value: object) -> str:
    digest = hashlib.sha256(f"{provider}\0{kind}\0{value}".encode()).hexdigest()[:24]
    return f"{provider}:{kind}:{digest}"


def _fragment(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    checked = _LABEL_RE.sub(" ", value).strip()
    return checked[:60] or fallback


def _timestamp(now: datetime | None) -> str:
    value = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _event(
    *,
    timestamp: str,
    phase: str,
    kind: str,
    trace_id: str,
    span_id: str | None,
    parent_span_id: str | None,
    actor: dict,
    context: dict,
    status: str,
    name: str,
    attributes: dict | None = None,
) -> dict:
    return validate_record(
        {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "timestamp": timestamp,
            "phase": phase,
            "kind": kind,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "actor": actor,
            "context": context,
            "status": status,
            "name": name,
            "attributes": attributes or {},
        }
    )


def normalize_payload(
    provider: str,
    payload: dict,
    metadata: dict,
    *,
    now: datetime | None = None,
    correlation_dir: Path | None = None,
) -> list[dict]:
    """Return safe events only. Unknown payload content is never copied."""
    provider = provider.lower()
    if provider not in {"claude", "codex"}:
        return []
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str):
        return []
    source = payload.get("source")
    if event_name == "SessionStart" and source == "compact":
        return []

    session_raw = payload.get("session_id")
    turn_raw = (
        payload.get("prompt_id") if provider == "claude" else payload.get("turn_id")
    )
    agent_sid = metadata.get("agent_sid")
    actor_id = agent_sid or metadata["timeline_actor_id"]
    session_scope = _namespace(provider, "session", session_raw or uuid.uuid4())
    inherited_trace = os.environ.get("MERLIN_TIMELINE_TRACE_ID")
    trace_id = (
        inherited_trace.strip()
        if isinstance(inherited_trace, str) and ID_RE.fullmatch(inherited_trace.strip())
        else session_scope
    )
    turn_scope = _namespace(provider, "turn", turn_raw) if turn_raw else None
    timestamp = _timestamp(now)
    provider_label = "Claude Code" if provider == "claude" else "Codex"
    actor_label = (
        f"{provider_label} · {_fragment(metadata.get('window_name'), 'Agent')}"
    )
    context = {
        "provider": provider_label,
        "model": _fragment(payload.get("model") or metadata.get("model"), "unknown"),
        "effort": _fragment(metadata.get("effort"), "unknown"),
        "project": _fragment(metadata.get("project"), "unknown"),
        "cwd": metadata.get("cwd"),
        "tmux_session": metadata.get("tmux_session"),
        "tmux_window": metadata.get("tmux_window"),
        "tmux_pane": metadata.get("tmux_pane"),
        "agent_sid": agent_sid,
    }
    context = {key: value for key, value in context.items() if value not in (None, "")}
    agent = {
        "type": "agent",
        "id": actor_id,
        "label": actor_label,
        "role": metadata.get("role"),
    }
    automation = {
        "type": "automation",
        "id": f"automation:{actor_id}",
        "label": "Automation",
    }
    human = {"type": "human", "id": "human", "label": "Human"}
    records: list[dict] = []

    def add(**kwargs) -> None:
        records.append(
            _event(timestamp=timestamp, trace_id=trace_id, context=context, **kwargs)
        )

    turn_key = f"turn:{provider}:{session_scope}:{actor_id}"
    if event_name == "SessionStart":
        clear_pending(
            f"turn:{provider}:{session_scope}:",
            f"tool:{provider}:{session_scope}:",
            f"permission:{provider}:{session_scope}:",
            directory=correlation_dir,
        )
        add(
            phase="point",
            kind="session.lifecycle",
            span_id=None,
            parent_span_id=None,
            actor=agent,
            status="ok",
            name="Session started" if source == "startup" else "Session resumed",
            attributes={
                "provider_event": "SessionStart",
                "session_source": _fragment(source, "unknown"),
            },
        )
        handoff_id = os.environ.get("MERLIN_TIMELINE_HANDOFF_ID")
        if handoff_id and consume_once(
            f"once:handoff:{trace_id}:{handoff_id}", directory=correlation_dir
        ):
            add(
                phase="point",
                kind="chain.handoff",
                span_id=None,
                parent_span_id=handoff_id,
                actor=automation,
                status="ok",
                name="Successor started",
                attributes={"provider_event": "SessionStart"},
            )
        return records

    if event_name == "UserPromptSubmit":
        add(
            phase="point",
            kind="human.prompt",
            span_id=None,
            parent_span_id=None,
            actor=human,
            status="ok",
            name="Prompt submitted",
            attributes={"provider_event": event_name},
        )
        created = True
        if turn_scope is None:
            turn_scope, created = remember_pending(turn_key, directory=correlation_dir)
        if not created:
            return records
        add(
            phase="start",
            kind="agent.turn",
            span_id=turn_scope,
            parent_span_id=None,
            actor=agent,
            status="running",
            name="Agent turn",
            attributes={"provider_event": event_name},
        )
        return records

    if event_name == "Stop":
        span_id = turn_scope or take_pending(turn_key, directory=correlation_dir)
        clear_pending(
            f"turn:{provider}:{session_scope}:",
            f"tool:{provider}:{session_scope}:",
            f"permission:{provider}:{session_scope}:",
            directory=correlation_dir,
        )
        if span_id is None:
            span_id = f"unmatched:{uuid.uuid4()}"
        add(
            phase="finish",
            kind="agent.turn",
            span_id=span_id,
            parent_span_id=None,
            actor=agent,
            status="ok",
            name="Agent turn complete",
            attributes={"provider_event": event_name},
        )
        return records

    tool_name = _fragment(payload.get("tool_name"), "tool")
    tool_raw = payload.get("tool_use_id")
    tool_scope = _namespace(provider, "tool", tool_raw) if tool_raw else None
    tool_key = f"tool:{provider}:{session_scope}:{turn_scope or 'missing'}:{tool_name}:{actor_id}"
    wait_key = (
        f"permission:{provider}:{session_scope}:{turn_scope or 'missing'}:{actor_id}"
    )
    is_question = tool_name in {"AskUserQuestion", "ExitPlanMode", "request_user_input"}

    if event_name in {"PermissionRequest", "Notification"}:
        if (
            event_name == "Notification"
            and payload.get("notification_type") != "permission_prompt"
        ):
            return []
        wait_span, created = remember_pending(wait_key, directory=correlation_dir)
        if created:
            add(
                phase="start",
                kind="agent.wait",
                span_id=wait_span,
                parent_span_id=turn_scope,
                actor=agent,
                status="blocked",
                name="Permission required",
                attributes={"provider_event": event_name},
            )
        return records

    if event_name == "PreToolUse":
        created = True
        if tool_scope is None:
            tool_scope, created = remember_pending(tool_key, directory=correlation_dir)
        if not created:
            return records
        add(
            phase="start",
            kind="agent.wait" if is_question else "tool.call",
            span_id=tool_scope,
            parent_span_id=turn_scope,
            actor=agent if is_question else automation,
            status="blocked" if is_question else "running",
            name="Waiting for answer" if is_question else f"Tool: {tool_name}",
            attributes={"provider_event": event_name, "tool_name": tool_name},
        )
        return records

    if event_name in {"PostToolUse", "PostToolUseFailure"}:
        if tool_scope is None:
            tool_scope = take_pending(tool_key, directory=correlation_dir)
        if tool_scope is None:
            tool_scope = f"unmatched:{uuid.uuid4()}"
        failed = event_name == "PostToolUseFailure" or payload.get("success") is False
        add(
            phase="finish",
            kind="agent.wait" if is_question else "tool.call",
            span_id=tool_scope,
            parent_span_id=turn_scope,
            actor=agent if is_question else automation,
            status="error" if failed else "ok",
            name=("Answer received" if is_question else f"Tool complete: {tool_name}"),
            attributes={"provider_event": event_name, "tool_name": tool_name},
        )
        permission_span = take_pending(wait_key, directory=correlation_dir)
        if permission_span:
            add(
                phase="finish",
                kind="agent.wait",
                span_id=permission_span,
                parent_span_id=turn_scope,
                actor=agent,
                status="ok",
                name="Permission resolved",
                attributes={"provider_event": event_name},
            )
        if is_question or permission_span:
            add(
                phase="point",
                kind="human.answer",
                span_id=None,
                parent_span_id=turn_scope,
                actor=human,
                status="ok",
                name="Answer submitted",
                attributes={"provider_event": event_name},
            )
    return records
