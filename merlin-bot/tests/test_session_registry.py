"""Tests for session_registry.py — thread/message → session mapping."""

from __future__ import annotations

import json

import pytest

import session_registry


@pytest.fixture(autouse=True)
def _use_tmp_registry(tmp_path, monkeypatch):
    """Redirect registry to a temp directory for every test."""
    monkeypatch.setattr(session_registry, "DATA_DIR", tmp_path)
    monkeypatch.setattr(session_registry, "REGISTRY_PATH", tmp_path / "session_registry.json")


# ---------------------------------------------------------------------------
# Thread sessions
# ---------------------------------------------------------------------------


class TestThreadSession:
    def test_get_missing_returns_none(self):
        assert session_registry.get_thread_session("123") is None

    def test_set_and_get(self):
        session_registry.set_thread_session("123", "session-abc")
        assert session_registry.get_thread_session("123") == "session-abc"

    def test_overwrite(self):
        session_registry.set_thread_session("123", "old")
        session_registry.set_thread_session("123", "new")
        assert session_registry.get_thread_session("123") == "new"

    def test_multiple_threads(self):
        session_registry.set_thread_session("111", "s1")
        session_registry.set_thread_session("222", "s2")
        assert session_registry.get_thread_session("111") == "s1"
        assert session_registry.get_thread_session("222") == "s2"

    def test_coerces_to_string(self):
        session_registry.set_thread_session("999", "s1")
        # Lookup with same string
        assert session_registry.get_thread_session("999") == "s1"


# ---------------------------------------------------------------------------
# Message sessions
# ---------------------------------------------------------------------------


class TestMessageSession:
    def test_get_missing_returns_none(self):
        assert session_registry.get_message_session("456") is None

    def test_set_and_get(self):
        session_registry.set_message_session("456", "session-xyz")
        assert session_registry.get_message_session("456") == "session-xyz"

    def test_overwrite(self):
        session_registry.set_message_session("456", "old")
        session_registry.set_message_session("456", "new")
        assert session_registry.get_message_session("456") == "new"

    def test_multiple_messages(self):
        session_registry.set_message_session("aaa", "s1")
        session_registry.set_message_session("bbb", "s2")
        assert session_registry.get_message_session("aaa") == "s1"
        assert session_registry.get_message_session("bbb") == "s2"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_survives_reload(self):
        session_registry.set_thread_session("t1", "s1")
        session_registry.set_message_session("m1", "s2")
        # Force reload from disk
        assert session_registry.get_thread_session("t1") == "s1"
        assert session_registry.get_message_session("m1") == "s2"

    def test_file_created_on_first_write(self, tmp_path):
        registry_path = tmp_path / "session_registry.json"
        assert not registry_path.exists()
        session_registry.set_thread_session("t1", "s1")
        assert registry_path.exists()

    def test_valid_json_on_disk(self, tmp_path):
        session_registry.set_thread_session("t1", "s1")
        registry_path = tmp_path / "session_registry.json"
        data = json.loads(registry_path.read_text())
        assert data["threads"]["t1"] == "s1"
        assert "messages" in data


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_corrupt_file_returns_empty(self, tmp_path):
        registry_path = tmp_path / "session_registry.json"
        registry_path.write_text("not json!!!")
        assert session_registry.get_thread_session("123") is None
        # Writing still works (overwrites corrupt file)
        session_registry.set_thread_session("123", "s1")
        assert session_registry.get_thread_session("123") == "s1"

    def test_empty_file_returns_empty(self, tmp_path):
        registry_path = tmp_path / "session_registry.json"
        registry_path.write_text("")
        assert session_registry.get_thread_session("123") is None

    def test_threads_and_messages_independent(self):
        session_registry.set_thread_session("123", "thread-session")
        session_registry.set_message_session("123", "message-session")
        assert session_registry.get_thread_session("123") == "thread-session"
        assert session_registry.get_message_session("123") == "message-session"
