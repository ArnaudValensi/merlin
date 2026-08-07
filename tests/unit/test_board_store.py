"""Tests for the board metadata store (board/store.py)."""

import pytest

from board import store as store_mod
from board.store import Session, Store


@pytest.fixture
def board_file(tmp_path, monkeypatch):
    p = tmp_path / "board.json"
    monkeypatch.setattr(store_mod, "store_path", lambda: p)
    return p


class TestLoadSave:
    def test_missing_file_is_empty_store(self, board_file):
        st = store_mod.load()
        assert st.sessions == {}

    def test_round_trip(self, board_file):
        st = Store(
            sessions={
                "s1": Session(sid="s1", name="cool", order=2.0, cwd="/a/b", project="b")
            }
        )
        store_mod.save(st)
        back = store_mod.load()
        assert back.sessions["s1"].name == "cool"
        assert back.sessions["s1"].order == 2.0
        assert back.sessions["s1"].project == "b"

    def test_corrupt_file_is_empty_store(self, board_file):
        board_file.write_text("{ not json")
        assert store_mod.load().sessions == {}

    def test_unknown_keys_ignored(self, board_file):
        board_file.write_text(
            '{"version": 1, "sessions": {"s1": {"sid": "s1", "bogus": 42}}}'
        )
        st = store_mod.load()
        assert st.sessions["s1"].sid == "s1"

    def test_non_dict_root_is_empty(self, board_file):
        board_file.write_text("[]")
        assert store_mod.load().sessions == {}

    def test_save_is_0600(self, board_file):
        store_mod.save(Store(sessions={"s1": Session(sid="s1")}))
        assert (board_file.stat().st_mode & 0o777) == 0o600


class TestTransaction:
    def test_transaction_persists(self, board_file):
        with store_mod.transaction() as st:
            st.sessions["s1"] = Session(sid="s1", name="x")
        assert store_mod.load().sessions["s1"].name == "x"
