"""Tests for merlin_app.py — bot monitoring endpoints.

The session viewer (filename validation + JSONL reading) moved to the core
`sessions` module; see tests/unit/test_sessions.py.
"""

import json

import pytest

import merlin_app as db


@pytest.fixture(autouse=True)
def _redirect_engine_log(tmp_path, monkeypatch):
    """Redirect ENGINE_LOG_PATH to a temp file so the monitoring endpoints
    read an isolated log, not whatever the import-time MERLIN_HOME pointed at."""
    from lib import event_log

    log_path = tmp_path / "engine-log.jsonl"
    monkeypatch.setattr(db, "ENGINE_LOG_PATH", log_path)
    # api_health/api_invocations/api_events read via the shared reader, which
    # resolves its own ENGINE_LOG_PATH; patch it too for isolation.
    monkeypatch.setattr(event_log, "ENGINE_LOG_PATH", log_path)
    return log_path


# ---------------------------------------------------------------------------
# Structured log: session_file and cost_usd fields
# ---------------------------------------------------------------------------


class TestStructuredLogFields:
    """Bot dict-adapter (over the shared reader) surfaces session_file/cost_usd."""

    def test_events_with_session_file(self, tmp_path, monkeypatch):
        """Events with session_file field are read correctly as dicts."""
        from lib import event_log

        log_path = tmp_path / "engine-log.jsonl"
        # The shared reader resolves the path from lib.event_log.
        monkeypatch.setattr(event_log, "ENGINE_LOG_PATH", log_path)

        event = {
            "type": "invocation",
            "timestamp": "2026-02-06T12:00:00+00:00",
            "caller": "discord",
            "duration": 5.0,
            "exit_code": 0,
            "num_turns": 2,
            "tokens_in": 100,
            "tokens_out": 50,
            "session_id": "sess-abc",
            "model": "opus",
            "session_file": "2026-02-06_12-00-00-discord-sess-abc.jsonl",
            "cost_usd": 0.05,
        }
        log_path.write_text(json.dumps(event) + "\n")

        events = db._read_event_dicts()
        assert len(events) == 1
        assert events[0]["session_file"] == "2026-02-06_12-00-00-discord-sess-abc.jsonl"
        assert events[0]["cost_usd"] == 0.05
        # extra='allow' field survives the round-trip
        assert events[0]["num_turns"] == 2


# ---------------------------------------------------------------------------
# Health API: no tunnel fields
# ---------------------------------------------------------------------------


class TestHealthNoTunnelFields:
    """The bundled tunnel is gone; api_health must not report tunnel state."""

    def test_health_has_no_tunnel_fields(self):
        result = db.api_health()
        assert "tunnel_url" not in result
        assert "tunnel_status" not in result
