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
            "devices": 0,
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


# ---------------------------------------------------------------------------
# M3: push routes, the glue, the displayed-window registry
# ---------------------------------------------------------------------------

import notifications.routes as nroutes  # noqa: E402

SUB = {
    "endpoint": "https://push.example/abc",
    "keys": {"p256dh": "BPUB", "auth": "AUTH"},
}


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    """A sender for this test's home, with pywebpush replaced."""
    monkeypatch.setattr(nroutes, "_sender", None)
    sender = nroutes.get_sender()
    rec = Recorder()
    monkeypatch.setattr(sender, "_send", rec)
    return rec


class TestPushRoutes:
    def test_public_key_is_stable_and_stored_0600(self, client, recorder):
        a = client.get("/api/notifications/public-key").json()["key"]
        b = client.get("/api/notifications/public-key").json()["key"]
        assert a == b and len(a) > 60
        import os
        import stat

        path = nroutes.notifications_dir() / "vapid.json"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_subscribe_list_and_unsubscribe(self, client, recorder):
        r = client.post(
            "/api/notifications/subscribe",
            json={"subscription": SUB, "label": "  Pixel   Chrome "},
        )
        assert r.status_code == 200
        assert r.json()["device"]["label"] == "Pixel Chrome"
        devices = client.get("/api/notifications/devices").json()["devices"]
        assert [d["endpoint"] for d in devices] == [SUB["endpoint"]]
        assert "keys" not in devices[0]
        assert client.get("/api/notifications/status").json()["devices"] == 1
        r = client.request(
            "DELETE", "/api/notifications/subscribe", json={"endpoint": SUB["endpoint"]}
        )
        assert r.json() == {"ok": True, "removed": True}
        assert client.get("/api/notifications/devices").json()["devices"] == []

    def test_label_derived_from_the_user_agent(self, client, recorder):
        ua = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        )
        r = client.post(
            "/api/notifications/subscribe",
            json={"subscription": SUB},
            headers={"User-Agent": ua},
        )
        assert r.json()["device"]["label"] == "iPhone · Safari"
        assert (
            nroutes.device_label("Mozilla/5.0 (X11; Linux x86_64) Firefox/130.0")
            == "Linux · Firefox"
        )
        assert nroutes.device_label("") == "Device"

    def test_subscribe_rejects_a_bad_subscription(self, client, recorder):
        r = client.post(
            "/api/notifications/subscribe", json={"subscription": {"endpoint": "x"}}
        )
        assert r.status_code == 400

    def test_test_push_sends_with_ttl_and_urgency(self, client, recorder):
        client.post("/api/notifications/subscribe", json={"subscription": SUB})
        r = client.post("/api/notifications/test", json={})
        assert r.json() == {"ok": True, "sent": 1, "failed": 0, "removed": 0}
        (call,) = recorder.calls
        assert call["ttl"] == 300 and call["headers"] == {"Urgency": "high"}
        import json as _json

        assert _json.loads(call["data"])["title"] == "Merlin · test"

    def test_test_push_without_devices_is_409_and_unknown_is_404(
        self, client, recorder
    ):
        assert client.post("/api/notifications/test", json={}).status_code == 409
        client.post("/api/notifications/subscribe", json={"subscription": SUB})
        r = client.post(
            "/api/notifications/test", json={"endpoint": "https://push.example/nope"}
        )
        assert r.status_code == 404

    def test_push_routes_require_auth(self, monkeypatch, recorder):
        import auth

        monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
        auth.configure("secret")
        with TestClient(app_mod.app) as c:
            for method, path in (
                ("GET", "/api/notifications/public-key"),
                ("GET", "/api/notifications/devices"),
                ("POST", "/api/notifications/subscribe"),
                ("DELETE", "/api/notifications/subscribe"),
                ("POST", "/api/notifications/test"),
            ):
                r = c.request(method, path, json={}, follow_redirects=False)
                assert r.status_code == 303, path


class TestGlue:
    def test_wire_push_subscribes_once_and_delivers_on_the_loop(
        self, monkeypatch, recorder
    ):
        from notifications.watcher import Watcher

        w = Watcher(lambda: [])
        monkeypatch.setattr(wmod, "watcher", w)
        nroutes.wire_push()
        nroutes.wire_push()
        assert w._listeners.count(nroutes._on_event) == 1
        nroutes.get_sender().store.add(SUB, "a")

        async def run():
            await w.tick()  # nothing yet: the scripted sweep is empty
            w.observe([win(sid="z", state="done")])
            await asyncio.sleep(0.05)

        import asyncio

        asyncio.run(run())
        assert len(recorder.calls) == 1
        assert recorder.calls[0]["subscription_info"]["endpoint"] == SUB["endpoint"]

    def test_listener_without_a_loop_is_a_no_op(self, recorder):
        from notifications.watcher import Watcher

        w = Watcher(lambda: [])
        w.add_listener(nroutes._on_event)
        w.observe([win(sid="q", state="done")])  # no running loop: nothing scheduled
        assert recorder.calls == []


class TestDisplayedTargets:
    def test_registry_reflects_connected_clients(self):
        from board.sweep import ClientSession
        from terminal import routes as troutes

        state = troutes.SessionReportState()
        troutes._client_views.add(state)
        try:
            assert troutes.is_displayed("alpha:@1") is False
            state.last_reported = ClientSession("alpha", "$1", 1, "@1", 0, "claude")
            assert troutes.displayed_targets() == {"alpha:@1"}
            assert troutes.is_displayed("alpha:@1") is True
        finally:
            troutes._client_views.discard(state)
        assert troutes.is_displayed("alpha:@1") is False
