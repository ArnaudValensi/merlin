"""Tests for lib/event_log.py — the typed, resilient engine-log reader."""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from lib import event_log as el
from lib.event_log import BotEvent, InvocationEvent


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
    """Build one invocation JSONL line."""
    event = {
        "type": "invocation",
        "timestamp": "2026-05-01T12:00:00+00:00",
        "caller": "discord",
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


def test_read_events_parses_valid_invocation(_isolated_log):
    _write(
        _isolated_log,
        _invocation(
            caller="cron-weather",
            duration=12.5,
            exit_code=0,
            cost_usd=0.05,
            session_file="2026-05-01_12-00-00-cron-weather-abc.jsonl",
        ),
    )

    events = el.read_events()
    assert len(events) == 1
    inv = events[0]
    assert isinstance(inv, InvocationEvent)
    assert inv.type == "invocation"
    assert inv.caller == "cron-weather"
    assert inv.duration == 12.5
    assert inv.exit_code == 0
    assert inv.tokens_in == 100
    assert inv.tokens_out == 50
    assert inv.cost_usd == 0.05
    assert inv.session_file == "2026-05-01_12-00-00-cron-weather-abc.jsonl"


def test_read_events_skips_malformed_json(_isolated_log):
    _write(
        _isolated_log,
        _invocation(caller="cron-a"),
        "this is not json {{{",
        _invocation(caller="cron-b"),
    )

    events = el.read_events()
    assert len(events) == 2
    assert [e.caller for e in events] == ["cron-a", "cron-b"]


def test_read_events_skips_validation_failures(_isolated_log, caplog):
    """A line whose duration is non-numeric fails validation and is skipped."""
    _write(
        _isolated_log,
        _invocation(caller="cron-good"),
        _invocation(caller="cron-bad", duration="not a number"),
    )

    with caplog.at_level(logging.WARNING, logger="merlin.event_log"):
        events = el.read_events()

    assert len(events) == 1
    assert events[0].caller == "cron-good"
    assert "skipped 1 malformed lines" in caplog.text


def test_read_events_filters_by_event_type(_isolated_log):
    _write(
        _isolated_log,
        _invocation(caller="cron-a"),
        json.dumps(
            {
                "type": "cron_dispatch",
                "timestamp": "2026-05-01T12:00:00+00:00",
                "job_id": "weather",
                "event": "completed",
            }
        ),
        json.dumps(
            {
                "type": "bot_event",
                "timestamp": "2026-05-01T12:00:00+00:00",
                "event": "ready",
            }
        ),
    )

    invocations = el.read_events(event_type="invocation")
    assert len(invocations) == 1
    assert all(e.type == "invocation" for e in invocations)

    bot_events = el.read_events(event_type="bot_event")
    assert len(bot_events) == 1
    assert isinstance(bot_events[0], BotEvent)
    assert bot_events[0].event == "ready"


def test_read_events_filters_by_since(_isolated_log):
    old = datetime(2026, 5, 1, tzinfo=timezone.utc)
    recent = datetime(2026, 5, 20, tzinfo=timezone.utc)
    _write(
        _isolated_log,
        _invocation(caller="cron-old", timestamp=old.isoformat()),
        _invocation(caller="cron-new", timestamp=recent.isoformat()),
    )

    cutoff = datetime(2026, 5, 10, tzinfo=timezone.utc)
    events = el.read_events(since=cutoff)
    assert len(events) == 1
    assert events[0].caller == "cron-new"


def test_read_events_filters_by_since_and_until(_isolated_log):
    base = datetime(2026, 5, 15, tzinfo=timezone.utc)
    _write(
        _isolated_log,
        _invocation(
            caller="cron-before", timestamp=(base - timedelta(days=5)).isoformat()
        ),
        _invocation(caller="cron-inside", timestamp=base.isoformat()),
        _invocation(
            caller="cron-after", timestamp=(base + timedelta(days=5)).isoformat()
        ),
    )

    events = el.read_events(
        since=base - timedelta(days=1), until=base + timedelta(days=1)
    )
    assert [e.caller for e in events] == ["cron-inside"]


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
        _invocation(caller="cron-a"),
        "garbage line one",
        "garbage line two",
        _invocation(caller="cron-b", duration="nope"),  # validation failure
        _invocation(caller="cron-c"),
    )

    with caplog.at_level(logging.WARNING, logger="merlin.event_log"):
        events = el.read_events()

    # 2 valid (cron-a, cron-c); 3 skipped (2 garbage + 1 validation failure).
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
        _invocation(caller="cron-old", timestamp=old.isoformat()),
        _invocation(caller="cron-new", timestamp=recent.isoformat()),
    )

    naive_cutoff = datetime(2026, 5, 10)  # no tzinfo
    events = el.read_events(since=naive_cutoff)
    assert [e.caller for e in events] == ["cron-new"]


def test_read_events_survives_non_utf8(_isolated_log):
    """A corrupt non-UTF-8 byte run is counted malformed, never crashes the read."""
    good = _invocation(caller="cron-ok").encode("utf-8")
    _isolated_log.write_bytes(good + b"\n" + b"\xff\xfe not utf8 \x80\n" + good + b"\n")

    events = el.read_events()  # must not raise
    assert len(events) == 2
    assert all(e.caller == "cron-ok" for e in events)


def test_read_events_no_warning_when_all_valid(_isolated_log, caplog):
    """A clean read must not emit a malformed-lines warning."""
    _write(_isolated_log, _invocation(caller="cron-a"), _invocation(caller="cron-b"))

    with caplog.at_level(logging.WARNING, logger="merlin.event_log"):
        events = el.read_events()

    assert len(events) == 2
    assert "malformed" not in caplog.text
