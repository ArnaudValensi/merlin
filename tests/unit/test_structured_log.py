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
    log_path = tmp_path / "events.jsonl"
    monkeypatch.setattr(sl, "EVENT_LOG_PATH", log_path)
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
        deep_path = tmp_path / "a" / "b" / "c" / "events.jsonl"
        monkeypatch.setattr(sl, "EVENT_LOG_PATH", deep_path)
        sl.log_event("test", foo="bar")
        assert deep_path.exists()

    def test_handles_non_serializable_values(self, _isolated_log):
        """datetime and other types are serialized via default=str."""
        sl.log_event("test", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
        events = _read_events(_isolated_log)
        assert "2026-01-01" in events[0]["when"]


class TestRetireOldLogs:
    def _logs(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))
        logs = tmp_path / "logs"
        logs.mkdir()
        monkeypatch.setattr(sl, "EVENT_LOG_PATH", logs / "events.jsonl")
        return logs

    def test_moves_the_event_log_without_the_bot_events(self, monkeypatch, tmp_path):
        logs = self._logs(monkeypatch, tmp_path)
        (logs / "engine-log.jsonl").write_text(
            '{"type": "bot_event", "event": "error"}\n'
            '{"type": "app_started"}\n'
            "not json\n"
        )
        sl.retire_old_logs()
        assert not (logs / "engine-log.jsonl").exists()
        assert (logs / "events.jsonl").read_text() == (
            '{"type": "app_started"}\nnot json\n'
        )

    def test_appends_to_an_event_log_already_there(self, monkeypatch, tmp_path):
        logs = self._logs(monkeypatch, tmp_path)
        (logs / "events.jsonl").write_text('{"type": "app_stopped"}\n')
        (logs / "engine-log.jsonl").write_text('{"type": "app_started"}\n')
        sl.retire_old_logs()
        assert (logs / "events.jsonl").read_text().splitlines() == [
            '{"type": "app_stopped"}',
            '{"type": "app_started"}',
        ]

    def test_removes_only_what_the_old_model_wrote(self, monkeypatch, tmp_path):
        logs = self._logs(monkeypatch, tmp_path)
        for name in ("raw-sessions", "claude", "activity"):
            (logs / name).mkdir()
            (logs / name / "x.jsonl").write_text("x")
        (logs / "cron_runner.log").write_text("x")
        (logs / "merlin.log").write_text("x")
        (tmp_path / "sessions").mkdir()
        (tmp_path / "user.md").write_text("mine")
        sl.retire_old_logs()
        assert not (logs / "raw-sessions").exists()
        assert not (logs / "claude").exists()
        assert not (logs / "cron_runner.log").exists()
        assert not (tmp_path / "sessions").exists()
        assert (logs / "activity" / "x.jsonl").exists()
        assert (logs / "merlin.log").exists()
        assert (tmp_path / "user.md").read_text() == "mine"

    def test_a_retry_after_a_crash_doubles_nothing(self, monkeypatch, tmp_path):
        """Crash after the merge, before the removal: the next start sees both."""
        logs = self._logs(monkeypatch, tmp_path)
        event = '{"type": "job_dispatch", "timestamp": "t"}\n'
        (logs / "events.jsonl").write_text(event)
        (logs / "engine-log.jsonl").write_text(event)
        sl.retire_old_logs()
        assert (logs / "events.jsonl").read_text() == event
        assert not (logs / "engine-log.jsonl").exists()

    def test_a_downgrade_and_back_keeps_both_sides(self, monkeypatch, tmp_path):
        logs = self._logs(monkeypatch, tmp_path)
        (logs / "events.jsonl").write_text('{"type": "a"}\n')
        (logs / "engine-log.jsonl").write_text('{"type": "b"}\n')
        sl.retire_old_logs()
        assert (logs / "events.jsonl").read_text() == '{"type": "a"}\n{"type": "b"}\n'

    @pytest.mark.parametrize("kind", ["[]", "{}", "1", "null"])
    def test_an_odd_type_stops_nothing(self, monkeypatch, tmp_path, kind):
        logs = self._logs(monkeypatch, tmp_path)
        (logs / "raw-sessions").mkdir()
        odd = f'{{"type": {kind}}}'
        (logs / "engine-log.jsonl").write_text(f'{odd}\n{{"type": "app_started"}}\n')
        sl.retire_old_logs()
        assert (logs / "events.jsonl").read_text().splitlines()[0] == odd
        assert not (logs / "raw-sessions").exists()

    def test_retention_keeps_what_it_cannot_date(self, monkeypatch, tmp_path):
        logs = self._logs(monkeypatch, tmp_path)
        lines = ['{"timestamp": []}', "[1]", '{"type": "x"}']
        old = '{"timestamp": "2020-01-01T00:00:00+00:00"}'
        (logs / "events.jsonl").write_text("\n".join([*lines, old]) + "\n")
        sl.cleanup_old_logs()
        assert (logs / "events.jsonl").read_text().splitlines() == lines

    @pytest.mark.parametrize(
        "existing",
        ["", '{"type": "a"}', '{"type": "a"}\n'],
        ids=["empty", "unterminated", "terminated"],
    )
    def test_every_record_keeps_its_own_line(self, monkeypatch, tmp_path, existing):
        """No final newline on either side must not glue the next record on."""
        logs = self._logs(monkeypatch, tmp_path)
        (logs / "events.jsonl").write_text(existing)
        (logs / "engine-log.jsonl").write_text('{"type": "b"}')
        sl.retire_old_logs()
        sl.log_event("c")
        records = (logs / "events.jsonl").read_text().splitlines()
        assert [json.loads(r)["type"] for r in records][-2:] == ["b", "c"]

    def test_is_quiet_when_nothing_is_left(self, monkeypatch, tmp_path):
        logs = self._logs(monkeypatch, tmp_path)
        sl.retire_old_logs()
        assert list(logs.iterdir()) == []
