"""Tests for lib/structured_log.py, the JSONL event writer."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

import structured_log as sl


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    """Redirect structured log to a temp file for every test."""
    log_path = tmp_path / "engine-log.jsonl"
    monkeypatch.setattr(sl, "ENGINE_LOG_PATH", log_path)
    yield log_path


def _read_events(log_path: Path) -> list[dict]:
    """Read all events from the JSONL log file."""
    if not log_path.exists():
        return []
    return [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]


# ---------------------------------------------------------------------------
# Core log_event
# ---------------------------------------------------------------------------


class TestLogEvent:
    def test_creates_file(self, _isolated_log):
        sl.log_event("test_event", foo="bar")
        assert _isolated_log.exists()

    def test_writes_json_line(self, _isolated_log):
        sl.log_event("test_event", key="value")
        events = _read_events(_isolated_log)
        assert len(events) == 1

    def test_event_has_type(self, _isolated_log):
        sl.log_event("job_runner_crash", exit_code=1)
        events = _read_events(_isolated_log)
        assert events[0]["type"] == "job_runner_crash"

    def test_event_has_timestamp(self, _isolated_log):
        sl.log_event("app_started", event="ready")
        events = _read_events(_isolated_log)
        ts = events[0]["timestamp"]
        # Should be valid ISO 8601
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None  # timezone-aware

    def test_timestamp_is_utc(self, _isolated_log):
        sl.log_event("app_started", event="ready")
        events = _read_events(_isolated_log)
        dt = datetime.fromisoformat(events[0]["timestamp"])
        assert dt.utcoffset().total_seconds() == 0

    def test_custom_fields_included(self, _isolated_log):
        sl.log_event(
            "webhook_request", job_id="weather", outcome="fired", ip="10.0.0.1"
        )
        events = _read_events(_isolated_log)
        assert events[0]["job_id"] == "weather"
        assert events[0]["outcome"] == "fired"
        assert events[0]["ip"] == "10.0.0.1"

    def test_multiple_events_appended(self, _isolated_log):
        sl.log_event("app_started", event="ready")
        sl.log_event("job_runner_crash", exit_code=1)
        sl.log_event("job_dispatch", job_id="weather")
        events = _read_events(_isolated_log)
        assert len(events) == 3
        assert [e["type"] for e in events] == [
            "app_started",
            "job_runner_crash",
            "job_dispatch",
        ]

    def test_thread_safety(self, _isolated_log):
        """Multiple threads writing simultaneously don't corrupt the file."""
        errors = []

        def write_events(n):
            try:
                for i in range(20):
                    sl.log_event("test", thread=n, index=i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_events, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        events = _read_events(_isolated_log)
        assert len(events) == 100  # 5 threads * 20 events

    def test_creates_parent_directories(self, tmp_path, monkeypatch):
        deep_path = tmp_path / "a" / "b" / "c" / "engine-log.jsonl"
        monkeypatch.setattr(sl, "ENGINE_LOG_PATH", deep_path)
        sl.log_event("test", foo="bar")
        assert deep_path.exists()

    def test_handles_non_serializable_values(self, _isolated_log):
        """datetime and other types are serialized via default=str."""
        sl.log_event("test", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
        events = _read_events(_isolated_log)
        assert "2026-01-01" in events[0]["when"]
