# /// script
# dependencies = ["pytest"]
# ///
"""Tests for structured_log.py and its wiring into wrapper/merlin/cron."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

import structured_log as sl


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    """Redirect structured log to a temp file for every test."""
    log_path = tmp_path / "structured.jsonl"
    monkeypatch.setattr(sl, "STRUCTURED_LOG_PATH", log_path)
    yield log_path


def _read_events(log_path: Path) -> list[dict]:
    """Read all events from the JSONL log file."""
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


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
        sl.log_event("invocation", caller="test")
        events = _read_events(_isolated_log)
        assert events[0]["type"] == "invocation"

    def test_event_has_timestamp(self, _isolated_log):
        sl.log_event("bot_event", event="ready")
        events = _read_events(_isolated_log)
        ts = events[0]["timestamp"]
        # Should be valid ISO 8601
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None  # timezone-aware

    def test_timestamp_is_utc(self, _isolated_log):
        sl.log_event("bot_event", event="ready")
        events = _read_events(_isolated_log)
        dt = datetime.fromisoformat(events[0]["timestamp"])
        assert dt.utcoffset().total_seconds() == 0

    def test_custom_fields_included(self, _isolated_log):
        sl.log_event("invocation", caller="discord", duration=12.5, exit_code=0)
        events = _read_events(_isolated_log)
        assert events[0]["caller"] == "discord"
        assert events[0]["duration"] == 12.5
        assert events[0]["exit_code"] == 0

    def test_multiple_events_appended(self, _isolated_log):
        sl.log_event("bot_event", event="ready")
        sl.log_event("invocation", caller="test")
        sl.log_event("cron_dispatch", job_id="weather")
        events = _read_events(_isolated_log)
        assert len(events) == 3
        assert [e["type"] for e in events] == ["bot_event", "invocation", "cron_dispatch"]

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
        deep_path = tmp_path / "a" / "b" / "c" / "structured.jsonl"
        monkeypatch.setattr(sl, "STRUCTURED_LOG_PATH", deep_path)
        sl.log_event("test", foo="bar")
        assert deep_path.exists()

    def test_handles_non_serializable_values(self, _isolated_log):
        """datetime and other types are serialized via default=str."""
        sl.log_event("test", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
        events = _read_events(_isolated_log)
        assert "2026-01-01" in events[0]["when"]


# ---------------------------------------------------------------------------
# Wiring: claude_wrapper emits invocation events
# ---------------------------------------------------------------------------

class TestClaudeWrapperWiring:
    def test_successful_invocation_emits_event(self, _isolated_log, tmp_path, monkeypatch):
        import claude_wrapper as cw
        monkeypatch.setattr(cw, "LOG_DIR", tmp_path / "claude")
        monkeypatch.setattr(cw, "SESSION_DIR", tmp_path / "sessions")
        monkeypatch.setattr(sl, "STRUCTURED_LOG_PATH", _isolated_log)

        # stream-json NDJSON format
        init = json.dumps({"type": "system", "subtype": "init", "model": "test-model", "session_id": "s1"})
        result = json.dumps({
            "type": "result", "subtype": "success", "result": "ok",
            "session_id": "s1", "num_turns": 3,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "total_cost_usd": 0.05,
            "modelUsage": {"test-model": {}},
        })
        stdout = init + "\n" + result + "\n"

        with mock.patch("subprocess.run", return_value=mock.Mock(
            stdout=stdout, stderr="", returncode=0
        )):
            cw.invoke_claude("hello", caller="discord")

        events = _read_events(_isolated_log)
        invocations = [e for e in events if e["type"] == "invocation"]
        assert len(invocations) == 1
        inv = invocations[0]
        assert inv["caller"] == "discord"
        assert inv["exit_code"] == 0
        assert inv["num_turns"] == 3
        assert inv["tokens_in"] == 100
        assert inv["tokens_out"] == 50
        assert inv["model"] == "test-model"
        assert inv["duration"] >= 0
        assert inv["cost_usd"] == 0.05
        assert inv["session_file"] is not None

    def test_error_invocation_emits_event(self, _isolated_log, tmp_path, monkeypatch):
        import claude_wrapper as cw
        monkeypatch.setattr(cw, "LOG_DIR", tmp_path / "claude")
        monkeypatch.setattr(cw, "SESSION_DIR", tmp_path / "sessions")
        monkeypatch.setattr(sl, "STRUCTURED_LOG_PATH", _isolated_log)

        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            cw.invoke_claude("hello", caller="test")

        events = _read_events(_isolated_log)
        invocations = [e for e in events if e["type"] == "invocation"]
        assert len(invocations) == 1
        assert invocations[0]["exit_code"] == 127

    def test_timeout_invocation_emits_event(self, _isolated_log, tmp_path, monkeypatch):
        import subprocess
        import claude_wrapper as cw
        monkeypatch.setattr(cw, "LOG_DIR", tmp_path / "claude")
        monkeypatch.setattr(cw, "SESSION_DIR", tmp_path / "sessions")
        monkeypatch.setattr(sl, "STRUCTURED_LOG_PATH", _isolated_log)

        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 10)):
            cw.invoke_claude("hello", caller="test", timeout=10)

        events = _read_events(_isolated_log)
        invocations = [e for e in events if e["type"] == "invocation"]
        assert len(invocations) == 1
        assert invocations[0]["exit_code"] == 124
