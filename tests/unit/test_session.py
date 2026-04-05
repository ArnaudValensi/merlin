"""Tests for lib/session.py — JSONL session management."""

import json
from datetime import datetime, timezone

import pytest

import lib.session as sess
from lib.session import (
    append_turn,
    compact_history,
    create_session,
    get_session_path,
    load_session,
    load_session_header,
    session_exists,
)


@pytest.fixture(autouse=True)
def _use_tmp_sessions(tmp_path, monkeypatch):
    """Redirect sessions_dir to tmp_path for all tests."""
    import paths
    monkeypatch.setattr(paths, "sessions_dir", lambda: tmp_path)


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_creates_file(self, tmp_path):
        path = create_session("test-1", engine="claude-code", model="opus")
        assert path.exists()

    def test_header_format(self, tmp_path):
        create_session("test-2", engine="claude-code", model="opus")
        path = get_session_path("test-2")
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        header = json.loads(lines[0])
        assert header["v"] == 1
        assert header["session_id"] == "test-2"
        assert header["engine"] == "claude-code"
        assert header["model"] == "opus"
        assert "created_at" in header

    def test_does_not_overwrite(self, tmp_path):
        create_session("test-3", engine="claude-code")
        append_turn("test-3", {"role": "user", "content": "hello"})
        create_session("test-3", engine="opencode")  # Should not overwrite
        turns = load_session("test-3")
        assert len(turns) == 1  # Turn still there


# ---------------------------------------------------------------------------
# append_turn
# ---------------------------------------------------------------------------


class TestAppendTurn:
    def test_appends_to_existing(self, tmp_path):
        create_session("s1")
        append_turn("s1", {"role": "user", "content": "hello"})
        append_turn("s1", {"role": "assistant", "content": "hi"})
        turns = load_session("s1")
        assert len(turns) == 2
        assert turns[0]["content"] == "hello"
        assert turns[1]["content"] == "hi"

    def test_auto_creates_session(self, tmp_path):
        """append_turn creates session if it doesn't exist."""
        append_turn("s2", {"role": "user", "content": "hello"})
        assert session_exists("s2")
        turns = load_session("s2")
        assert len(turns) == 1

    def test_adds_timestamp(self, tmp_path):
        create_session("s3")
        append_turn("s3", {"role": "user", "content": "hello"})
        turns = load_session("s3")
        assert "ts" in turns[0]

    def test_preserves_existing_timestamp(self, tmp_path):
        create_session("s4")
        append_turn("s4", {"role": "user", "content": "hello", "ts": "2026-01-01T00:00:00+00:00"})
        turns = load_session("s4")
        assert turns[0]["ts"] == "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# load_session
# ---------------------------------------------------------------------------


class TestLoadSession:
    def test_load_empty_session(self, tmp_path):
        create_session("empty")
        turns = load_session("empty")
        assert turns == []

    def test_load_with_turns(self, tmp_path):
        create_session("full")
        append_turn("full", {"role": "user", "content": "q1"})
        append_turn("full", {"role": "assistant", "content": "a1"})
        turns = load_session("full")
        assert len(turns) == 2
        assert turns[0]["role"] == "user"
        assert turns[1]["role"] == "assistant"

    def test_load_nonexistent_returns_empty(self, tmp_path):
        turns = load_session("doesnt-exist")
        assert turns == []

    def test_skips_header(self, tmp_path):
        create_session("hdr", engine="test")
        append_turn("hdr", {"role": "user", "content": "hello"})
        turns = load_session("hdr")
        assert len(turns) == 1  # Header not included
        assert turns[0]["role"] == "user"

    def test_skips_invalid_json_lines(self, tmp_path):
        create_session("bad")
        path = get_session_path("bad")
        with open(path, "a") as f:
            f.write("not json\n")
            f.write(json.dumps({"role": "user", "content": "ok"}) + "\n")
        turns = load_session("bad")
        assert len(turns) == 1
        assert turns[0]["content"] == "ok"


# ---------------------------------------------------------------------------
# session_exists / get_session_path
# ---------------------------------------------------------------------------


class TestSessionExists:
    def test_exists(self, tmp_path):
        create_session("exists")
        assert session_exists("exists")

    def test_not_exists(self, tmp_path):
        assert not session_exists("nope")


class TestLoadSessionHeader:
    def test_loads_header(self, tmp_path):
        create_session("h1", engine="claude-code", model="opus")
        header = load_session_header("h1")
        assert header is not None
        assert header["engine"] == "claude-code"
        assert header["model"] == "opus"

    def test_nonexistent_returns_none(self, tmp_path):
        assert load_session_header("missing") is None


# ---------------------------------------------------------------------------
# compact_history
# ---------------------------------------------------------------------------


class TestCompactHistory:
    def test_no_compaction_needed(self):
        history = [
            {"role": "user", "content": "short"},
            {"role": "assistant", "content": "also short"},
        ]
        result = compact_history(history, max_tokens=10000)
        assert result is history  # Same object, unchanged

    def test_empty_history(self):
        result = compact_history([], max_tokens=100)
        assert result == []

    def test_drops_oldest_keeps_recent(self):
        history = [
            {"role": "system", "content": "You are a bot"},
        ]
        # Add 30 turns
        for i in range(30):
            history.append({"role": "user", "content": f"message {i}" * 100})
            history.append({"role": "assistant", "content": f"reply {i}" * 100})

        result = compact_history(history, max_tokens=1000, keep_recent=4)

        # Should have: system + compaction marker + 4 recent turns
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "compaction"
        assert result[1]["dropped"] == 56  # 60 rest turns - 4 kept
        assert len(result) == 6  # system + compaction + 4

    def test_system_prompt_always_preserved(self):
        history = [
            {"role": "system", "content": "Important system prompt" * 100},
        ]
        for i in range(30):
            history.append({"role": "user", "content": f"msg {i}" * 100})

        result = compact_history(history, max_tokens=500, keep_recent=5)
        assert result[0]["role"] == "system"
        assert result[0]["content"].startswith("Important system prompt")

    def test_compaction_marker_has_count(self):
        history = []
        for i in range(25):
            history.append({"role": "user", "content": f"msg {i}" * 100})

        result = compact_history(history, max_tokens=500, keep_recent=5)
        markers = [t for t in result if t.get("role") == "compaction"]
        assert len(markers) == 1
        assert markers[0]["dropped"] == 20

    def test_history_with_system_and_one_turn(self):
        """Edge case: system + 1 turn, can't drop anything."""
        history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        result = compact_history(history, max_tokens=1, keep_recent=20)
        assert result == history  # Nothing to drop (rest <= keep_recent)

    def test_no_system_turn(self):
        """History without system turn still compacts."""
        history = [
            {"role": "user", "content": f"msg {i}" * 100}
            for i in range(30)
        ]
        result = compact_history(history, max_tokens=500, keep_recent=5)
        assert result[0]["role"] == "compaction"
        assert result[0]["dropped"] == 25
        assert len(result) == 6  # compaction + 5
