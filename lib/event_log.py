"""Canonical reader for the engine event log (``logs/engine-log.jsonl``).

This is the read-side counterpart to ``lib/structured_log.py`` (the writer).
Writers stay free-form (``log_event(**fields)``); readers parse each JSONL line
into a typed Pydantic model so the documented schema lives in code without
forcing any writer change.

Design:
  - Every model sets ``extra="allow"`` so new writer fields never break readers.
  - ``type`` and ``timestamp`` are required on every event.
  - Lines that fail JSON decode or model validation are skipped and counted;
    a single ``WARNING`` summary is emitted at the end of a read.

Consumers:
  - ``merlin-bot/merlin_app.py`` (bot dashboard: health, invocations, logs)
  - ``cron/routes.py`` (cron crash banner + ``/api/cron/performance``)
  - ``perf/aggregate.py`` operates on the ``InvocationEvent`` models below.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, ValidationError

import paths

ENGINE_LOG_PATH = paths.logs_dir() / "engine-log.jsonl"

_logger = logging.getLogger("merlin.event_log")


# ---------------------------------------------------------------------------
# Event models — consumer-side schema for engine-log.jsonl
# ---------------------------------------------------------------------------


class BaseEvent(BaseModel):
    """Common shape for every engine-log event.

    ``extra="allow"`` keeps unknown / future fields accessible instead of
    dropping them, so adding a field to a writer never breaks a reader.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    timestamp: str


class InvocationEvent(BaseEvent):
    """An engine call (the actual AI invocation). Fields per docs/dev/logging-system.md.

    ``num_turns``, ``prompt`` and any other writer fields are preserved via
    ``extra="allow"`` even though they are not declared here.
    """

    caller: str | None = None
    duration: float = 0.0
    exit_code: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    model: str | None = None
    session_id: str | None = None
    session_file: str | None = None
    engine: str | None = None
    stderr: str = ""
    request_id: str | None = None


class CronDispatchEvent(BaseEvent):
    """A cron job execution lifecycle event (started / completed / failed)."""

    job_id: str | None = None
    duration: float | None = None
    exit_code: int | None = None


class CronRunnerCrashEvent(BaseEvent):
    """The cron runner subprocess died unexpectedly."""

    exit_code: int | None = None
    stderr: str = ""


class BotEvent(BaseEvent):
    """A Discord bot lifecycle event.

    The writer's discriminator field is ``event`` (ready / error /
    transcription / message_received), not ``subtype``; declaring it here keeps
    the real schema in code. Free-form payloads (``content``, ``author``,
    ``channel`` ...) flow through via ``extra="allow"``.
    """

    event: str | None = None
    details: str | None = None


class AppLifecycleEvent(BaseEvent):
    """Server start / stop. ``app_stopped`` carries no payload beyond the base."""

    host: str | None = None
    port: int | None = None
    cwd: str | None = None
    extensions: list[str] | None = None


# Concrete model per ``type``; unknown types fall back to BaseEvent so they are
# still returned (parity with the previous dict-based reader) rather than
# treated as malformed.
_MODEL_FOR_TYPE: dict[str, type[BaseEvent]] = {
    "invocation": InvocationEvent,
    "cron_dispatch": CronDispatchEvent,
    "cron_runner_crash": CronRunnerCrashEvent,
    "bot_event": BotEvent,
    "app_started": AppLifecycleEvent,
    "app_stopped": AppLifecycleEvent,
}


def _parse_ts(timestamp: str) -> datetime | None:
    """Parse an ISO 8601 timestamp; return None if it cannot be parsed."""
    try:
        return datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None


def read_events(
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[BaseEvent]:
    """Read typed events from ``engine-log.jsonl``, optionally filtered.

    Args:
        event_type: If given, only events whose ``type`` matches are returned.
            Type-mismatched lines are skipped silently (not counted malformed).
        since: If given, drop events with ``timestamp`` strictly before it.
        until: If given, drop events with ``timestamp`` strictly after it.

    Returns:
        A list of event models (subclasses of :class:`BaseEvent`). Each line is
        validated against the model for its ``type``; ``invocation`` lines come
        back as :class:`InvocationEvent`, and so on. Unknown types come back as
        :class:`BaseEvent`.

    Resilience:
        Missing, empty, or unreadable file returns ``[]`` (a transient I/O
        error or a corrupt/non-UTF-8 file never raises). Lines that fail JSON
        decoding or model validation are skipped and counted; if any were
        skipped, a single ``WARNING`` summary is logged. Naive ``since`` /
        ``until`` bounds are treated as UTC for comparison.
    """
    if not ENGINE_LOG_PATH.exists():
        return []

    # Engine timestamps are timezone-aware; coerce naive bounds to UTC so a
    # naive since/until never raises TypeError when compared against them.
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    try:
        # errors="replace" keeps a corrupt / non-UTF-8 byte run from raising:
        # affected lines then fail JSON decode and are counted as malformed
        # rather than crashing the read.
        text = ENGINE_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # File removed between the exists() check and the read, or a transient
        # I/O error: degrade to empty, matching the missing-file contract.
        return []

    events: list[BaseEvent] = []
    malformed = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue

        # A JSONL line must be an object; anything else (array, scalar) is junk.
        if not isinstance(data, dict):
            malformed += 1
            continue

        etype = data.get("type")
        if event_type is not None and etype != event_type:
            continue

        model_cls = _MODEL_FOR_TYPE.get(etype, BaseEvent)
        try:
            event = model_cls.model_validate(data)
        except ValidationError:
            malformed += 1
            continue

        if since is not None or until is not None:
            ts = _parse_ts(event.timestamp)
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if since is not None and ts < since:
                continue
            if until is not None and ts > until:
                continue

        events.append(event)

    if malformed:
        _logger.warning("skipped %d malformed lines in engine-log.jsonl", malformed)

    return events
