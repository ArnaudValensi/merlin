"""Tests for the notifications wiring: the board poll's cursor transport, the
status route, its auth, and the app lifespan stopping the watcher."""

from unittest import mock

import pytest
from fastapi.testclient import TestClient

import main as app_mod
import notifications.watcher as wmod
from board.sweep import Window
from notifications.watcher import Watcher


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


@pytest.fixture
def client():
    with TestClient(app_mod.app) as c:
        yield c


def win(sid="s1", state="done", wid="@1", session="alpha", cwd="/h/u/proj"):
    return Window(
        sid=sid,
        state=state,
        cwd=cwd,
        parent="",
        relation="",
        session=session,
        window_id=wid,
        index=1,
        active=False,
        activity=0,
        name="claude",
    )


@pytest.fixture
def fresh_watcher(monkeypatch):
    w = Watcher(lambda: [])
    monkeypatch.setattr(wmod, "watcher", w)
    return w


@pytest.fixture
def no_tmux(monkeypatch):
    from board import routes as broutes

    monkeypatch.setattr(broutes.sweep, "run_session_sweep", lambda: [])
    monkeypatch.setattr(broutes.sweep, "run_sweep", lambda: [])


class TestBoardPollTransport:
    def test_first_poll_without_cursor_gets_no_events_and_a_cursor(
        self, client, fresh_watcher, no_tmux
    ):
        fresh_watcher.observe([win()])  # an event exists before the page loads
        r = client.get("/api/board")
        assert r.status_code == 200
        body = r.json()
        assert body["events"] == []
        assert body["cursor"] == fresh_watcher.cursor
        assert body["dropped"] == 0
        assert "sessions" in body  # the tree is untouched

    def test_events_after_the_cursor_are_returned_once(
        self, client, fresh_watcher, no_tmux
    ):
        c0 = client.get("/api/board").json()["cursor"]
        fresh_watcher.observe([win(sid="a")])
        fresh_watcher.observe([win(sid="a"), win(sid="b", state="ask", wid="@2")])
        body = client.get("/api/board", params={"since": c0}).json()
        assert [e["sid"] for e in body["events"]] == ["a", "b"]
        ev = body["events"][0]
        assert ev["target"] == "alpha:@1"
        assert ev["project"] == "proj"
        assert ev["state"] == "done"
        again = client.get("/api/board", params={"since": body["cursor"]}).json()
        assert again["events"] == []

    def test_dropped_count_is_reported(self, client, no_tmux, monkeypatch):
        w = Watcher(lambda: [], ring_size=2)
        monkeypatch.setattr(wmod, "watcher", w)
        c0 = client.get("/api/board").json()["cursor"]
        for i in range(4):
            w.observe([win(sid=f"s{i}")])
        body = client.get("/api/board", params={"since": c0}).json()
        assert body["dropped"] == 2
        assert [e["sid"] for e in body["events"]] == ["s2", "s3"]


class TestStatusRoute:
    def test_status_reports_the_watcher(self, client, fresh_watcher):
        r = client.get("/api/notifications/status")
        assert r.status_code == 200
        assert r.json() == {
            "tmux": None,
            "swept": False,
            "cursor": fresh_watcher.cursor,
        }
        fresh_watcher.observe(None)
        assert client.get("/api/notifications/status").json()["tmux"] is False

    def test_status_requires_auth(self, monkeypatch, fresh_watcher):
        import auth

        monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
        auth.configure("secret")
        with TestClient(app_mod.app) as c:
            r = c.get("/api/notifications/status", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"].startswith("/login")
            r = c.get("/api/board", follow_redirects=False)
            assert r.status_code == 303


class TestLifespan:
    def test_shutdown_stops_the_watcher(self, monkeypatch):
        stopped = mock.AsyncMock()
        monkeypatch.setattr(wmod, "stop", stopped)
        with TestClient(app_mod.app):
            stopped.assert_not_awaited()
        stopped.assert_awaited_once()

    def test_static_assets_are_served(self, client):
        for name in ("notifications.js", "notifications.css"):
            r = client.get(f"/static/notifications/{name}")
            assert r.status_code == 200, name
