"""Tests for lib/event_log.py — the typed, resilient engine-log reader."""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from lib import event_log as el
from lib.event_log import BaseEvent, WebhookRequestEvent


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    """Point event_log.ENGINE_LOG_PATH at a temp file for every test."""
    log_path = tmp_path / "engine-log.jsonl"
    monkeypatch.setattr(el, "ENGINE_LOG_PATH", log_path)
    return log_path


def _write(log_path, *lines: str) -> None:
    """Write raw JSONL lines (each already a string) to the log file."""
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _invocation(**overrides) -> str:
    """Build one JSONL line of a type the reader has no model for (such
    lines come back as BaseEvent with every field kept)."""
    event = {
        "type": "invocation",
        "timestamp": "2026-05-01T12:00:00+00:00",
        "caller": "job-weather",
        "duration": 4.2,
        "exit_code": 0,
        "tokens_in": 100,
        "tokens_out": 50,
        "cost_usd": 0.02,
        "model": "claude-opus-4-8",
    }
    event.update(overrides)
    return json.dumps(event)


# ---------------------------------------------------------------------------
# Empty / missing file
# ---------------------------------------------------------------------------


def test_read_events_empty_file_returns_empty_list(_isolated_log):
    _isolated_log.write_text("", encoding="utf-8")
    assert el.read_events() == []


def test_read_events_missing_file_returns_empty_list(_isolated_log):
    assert not _isolated_log.exists()
    assert el.read_events() == []


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_read_events_parses_a_typed_event(_isolated_log):
    _write(
        _isolated_log,
        json.dumps(
            {
                "type": "webhook_request",
                "timestamp": "2026-05-01T12:00:00+00:00",
                "job_id": "weather",
                "outcome": "fired",
                "ip": "10.0.0.1",
                "run_id": "abc",
            }
        ),
    )

    events = el.read_events()
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, WebhookRequestEvent)
    assert ev.type == "webhook_request"
    assert ev.job_id == "weather"
    assert ev.outcome == "fired"
    assert ev.ip == "10.0.0.1"
    assert ev.run_id == "abc"


def test_read_events_keeps_an_unknown_type_as_a_base_event(_isolated_log):
    """A line of a type without a model (one an older release wrote) comes
    back as BaseEvent, its fields reachable through the extras."""
    _write(_isolated_log, _invocation(caller="job-weather", duration=12.5))

    events = el.read_events()
    assert len(events) == 1
    ev = events[0]
    assert type(ev) is BaseEvent
    assert ev.type == "invocation"
    assert ev.caller == "job-weather"
    assert ev.duration == 12.5


def test_read_events_skips_malformed_json(_isolated_log):
    _write(
        _isolated_log,
        _invocation(caller="job-a"),
        "this is not json {{{",
        _invocation(caller="job-b"),
    )

    events = el.read_events()
    assert len(events) == 2
    assert [e.caller for e in events] == ["job-a", "job-b"]


def _started(**overrides) -> str:
    """Build one app_started JSONL line (a typed event: its port is an int)."""
    event = {
        "type": "app_started",
        "timestamp": "2026-05-01T12:00:00+00:00",
        "host": "0.0.0.0",
        "port": 3123,
    }
    event.update(overrides)
    return json.dumps(event)


def test_read_events_skips_validation_failures(_isolated_log, caplog):
    """A typed line whose port is not a number fails validation and is
    skipped."""
    _write(
        _isolated_log,
        _started(host="good"),
        _started(host="bad", port="not a number"),
    )

    with caplog.at_level(logging.WARNING, logger="merlin.event_log"):
        events = el.read_events()

    assert len(events) == 1
    assert events[0].host == "good"
    assert "skipped 1 malformed lines" in caplog.text


def test_read_events_filters_by_event_type(_isolated_log):
    _write(
        _isolated_log,
        _invocation(caller="job-a"),
        json.dumps(
            {
                "type": "job_dispatch",
                "timestamp": "2026-05-01T12:00:00+00:00",
                "job_id": "weather",
                "event": "completed",
            }
        ),
        json.dumps(
            {
                "type": "app_started",
                "timestamp": "2026-05-01T12:00:00+00:00",
                "port": 3123,
            }
        ),
    )

    invocations = el.read_events(event_type="invocation")
    assert len(invocations) == 1
    assert all(e.type == "invocation" for e in invocations)

    started = el.read_events(event_type="app_started")
    assert len(started) == 1
    assert started[0].port == 3123


def test_read_events_filters_by_since(_isolated_log):
    old = datetime(2026, 5, 1, tzinfo=timezone.utc)
    recent = datetime(2026, 5, 20, tzinfo=timezone.utc)
    _write(
        _isolated_log,
        _invocation(caller="job-old", timestamp=old.isoformat()),
        _invocation(caller="job-new", timestamp=recent.isoformat()),
    )

    cutoff = datetime(2026, 5, 10, tzinfo=timezone.utc)
    events = el.read_events(since=cutoff)
    assert len(events) == 1
    assert events[0].caller == "job-new"


def test_read_events_filters_by_since_and_until(_isolated_log):
    base = datetime(2026, 5, 15, tzinfo=timezone.utc)
    _write(
        _isolated_log,
        _invocation(
            caller="job-before", timestamp=(base - timedelta(days=5)).isoformat()
        ),
        _invocation(caller="job-inside", timestamp=base.isoformat()),
        _invocation(
            caller="job-after", timestamp=(base + timedelta(days=5)).isoformat()
        ),
    )

    events = el.read_events(
        since=base - timedelta(days=1), until=base + timedelta(days=1)
    )
    assert [e.caller for e in events] == ["job-inside"]


def test_read_events_extra_fields_preserved(_isolated_log):
    """Unknown fields survive via extra='allow' and stay accessible."""
    _write(
        _isolated_log,
        _invocation(num_turns=7, prompt="do the thing", brand_new_field="surprise"),
    )

    events = el.read_events()
    assert len(events) == 1
    dumped = events[0].model_dump()
    assert dumped["num_turns"] == 7
    assert dumped["prompt"] == "do the thing"
    assert dumped["brand_new_field"] == "surprise"
    # Also reachable as attributes (pydantic exposes extras as attributes).
    assert events[0].num_turns == 7


def test_read_events_logs_malformed_summary(_isolated_log, caplog):
    _write(
        _isolated_log,
        _invocation(caller="job-a"),
        "garbage line one",
        "garbage line two",
        _started(port="nope"),  # validation failure
        _invocation(caller="job-c"),
    )

    with caplog.at_level(logging.WARNING, logger="merlin.event_log"):
        events = el.read_events()

    # 2 valid (job-a, job-c); 3 skipped (2 garbage + 1 validation failure).
    assert len(events) == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "skipped 3 malformed lines in engine-log.jsonl" in caplog.text


def test_read_events_naive_since_coerced(_isolated_log):
    """A timezone-naive `since` must filter (treated as UTC), not raise TypeError."""
    old = datetime(2026, 5, 1, tzinfo=timezone.utc)
    recent = datetime(2026, 5, 20, tzinfo=timezone.utc)
    _write(
        _isolated_log,
        _invocation(caller="job-old", timestamp=old.isoformat()),
        _invocation(caller="job-new", timestamp=recent.isoformat()),
    )

    naive_cutoff = datetime(2026, 5, 10)  # no tzinfo
    events = el.read_events(since=naive_cutoff)
    assert [e.caller for e in events] == ["job-new"]


def test_read_events_survives_non_utf8(_isolated_log):
    """A corrupt non-UTF-8 byte run is counted malformed, never crashes the read."""
    good = _invocation(caller="job-ok").encode("utf-8")
    _isolated_log.write_bytes(good + b"\n" + b"\xff\xfe not utf8 \x80\n" + good + b"\n")

    events = el.read_events()  # must not raise
    assert len(events) == 2
    assert all(e.caller == "job-ok" for e in events)


def test_read_events_no_warning_when_all_valid(_isolated_log, caplog):
    """A clean read must not emit a malformed-lines warning."""
    _write(_isolated_log, _invocation(caller="job-a"), _invocation(caller="job-b"))

    with caplog.at_level(logging.WARNING, logger="merlin.event_log"):
        events = el.read_events()

    assert len(events) == 2
    assert "malformed" not in caplog.text
