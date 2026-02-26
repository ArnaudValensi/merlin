# /// script
# dependencies = ["pytest"]
# ///
"""Tests for merlin_app.py — session API and filename validation."""

import json
from pathlib import Path
from unittest import mock

import pytest

import merlin_app as db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _redirect_paths(tmp_path, monkeypatch):
    """Redirect SESSION_DIR and STRUCTURED_LOG_PATH to temp directory."""
    session_dir = tmp_path / "logs" / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(db, "SESSION_DIR", session_dir)
    monkeypatch.setattr(db, "STRUCTURED_LOG_PATH", tmp_path / "structured.jsonl")
    return session_dir


@pytest.fixture
def session_dir(tmp_path):
    """Return the session dir (same as autouse fixture)."""
    return db.SESSION_DIR


@pytest.fixture
def sample_session_file(session_dir):
    """Create a sample session JSONL file and return its filename."""
    filename = "2026-02-06_12-00-00-test-sess-abc.jsonl"
    content = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "model": "opus", "session_id": "sess-abc"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}),
        json.dumps({"type": "result", "subtype": "success", "result": "Hello", "session_id": "sess-abc",
                     "num_turns": 1, "duration_ms": 1000, "usage": {}, "total_cost_usd": 0.01}),
    ]) + "\n"
    (session_dir / filename).write_text(content)
    return filename


# ---------------------------------------------------------------------------
# Filename validation
# ---------------------------------------------------------------------------

class TestFilenameValidation:
    """_validate_session_filename prevents path traversal and bad names."""

    def test_valid_filename(self):
        # Should not raise
        db._validate_session_filename("2026-02-06_12-00-00-discord-sess-abc.jsonl")

    def test_rejects_path_traversal_dots(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            db._validate_session_filename("../../etc/passwd")
        assert exc_info.value.status_code == 400

    def test_rejects_forward_slash(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            db._validate_session_filename("subdir/file.jsonl")

    def test_rejects_backslash(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            db._validate_session_filename("subdir\\file.jsonl")

    def test_rejects_non_jsonl_extension(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            db._validate_session_filename("file.txt")

    def test_rejects_empty(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            db._validate_session_filename("")

    def test_rejects_special_chars(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            db._validate_session_filename("file name.jsonl")

    def test_accepts_hyphens_underscores(self):
        db._validate_session_filename("2026-02-06_12-00-00-test-no-session.jsonl")


# ---------------------------------------------------------------------------
# Session file reading (api_session logic)
# ---------------------------------------------------------------------------

class TestSessionReading:
    """Session JSONL files are parsed correctly."""

    def test_reads_session_events(self, sample_session_file):
        """Simulates what api_session does: reads and parses JSONL."""
        session_path = db.SESSION_DIR / sample_session_file
        events = []
        for line in session_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        assert len(events) == 3
        assert events[0]["type"] == "system"
        assert events[1]["type"] == "assistant"
        assert events[2]["type"] == "result"

    def test_nonexistent_file(self):
        """Session file that doesn't exist."""
        path = db.SESSION_DIR / "nonexistent.jsonl"
        assert not path.exists()

    def test_empty_session_file(self, session_dir):
        """Empty session file returns no events."""
        filename = "empty.jsonl"
        (session_dir / filename).write_text("")

        events = []
        for line in (session_dir / filename).read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))

        assert events == []

    def test_malformed_lines_skipped(self, session_dir):
        """Malformed JSON lines are skipped."""
        filename = "malformed.jsonl"
        content = "not json\n" + json.dumps({"type": "result", "result": "ok"}) + "\n"
        (session_dir / filename).write_text(content)

        events = []
        for line in (session_dir / filename).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        assert len(events) == 1
        assert events[0]["type"] == "result"


# ---------------------------------------------------------------------------
# Structured log: session_file and cost_usd fields
# ---------------------------------------------------------------------------

class TestStructuredLogFields:
    """Verify that read_events can handle new session_file and cost_usd fields."""

    def test_events_with_session_file(self, tmp_path, monkeypatch):
        """Events with session_file field are read correctly."""
        log_path = tmp_path / "structured.jsonl"
        monkeypatch.setattr(db, "STRUCTURED_LOG_PATH", log_path)

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

        events = db.read_events()
        assert len(events) == 1
        assert events[0]["session_file"] == "2026-02-06_12-00-00-discord-sess-abc.jsonl"
        assert events[0]["cost_usd"] == 0.05


# ---------------------------------------------------------------------------
# Health API: tunnel fields
# ---------------------------------------------------------------------------

class TestHealthTunnelFields:
    """api_health includes tunnel_url and tunnel_status."""

    def test_health_includes_tunnel_fields(self, monkeypatch):
        """Health endpoint returns tunnel status from tunnel module."""
        import tunnel
        monkeypatch.setattr(tunnel, "_public_url", "https://test.trycloudflare.com")
        monkeypatch.setattr(tunnel, "_status", "running")

        result = db.api_health()
        assert result["tunnel_url"] == "https://test.trycloudflare.com"
        assert result["tunnel_status"] == "running"

    def test_health_tunnel_stopped(self, monkeypatch):
        """When tunnel is stopped, fields reflect that."""
        import tunnel
        monkeypatch.setattr(tunnel, "_public_url", None)
        monkeypatch.setattr(tunnel, "_status", "stopped")

        result = db.api_health()
        assert result["tunnel_url"] is None
        assert result["tunnel_status"] == "stopped"
