"""Apps REST routes, pages and the signaling socket's auth, on a bare app."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import routes


@pytest.fixture
def client(monkeypatch):
    import auth

    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")
    app = FastAPI()
    app.include_router(routes.api_router, prefix="/api/apps")
    app.include_router(routes.page_router, prefix="/apps")
    routes.register_routes(app)
    with TestClient(app) as c:
        yield c
    auth.configure("")


def test_bitrate_scales_with_pixels():
    assert routes.bitrate_kbps(1920, 1080) == 8000
    assert routes.bitrate_kbps(1280, 720) == 8000 * 1280 * 720 // (1920 * 1080)
    assert routes.bitrate_kbps(320, 240) == 1500
    assert routes.bitrate_kbps(3840, 2160) == 12000


def test_socket_rejects_unauthenticated(client):
    import auth

    auth.configure("secret")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/apps/probe/stream") as ws:
            ws.receive_text()
    assert exc.value.code == 4401


def test_socket_reports_a_missing_app(client):
    with client.websocket_connect("/ws/apps/ghost/stream") as ws:
        assert ws.receive_json() == {"type": "exited", "reason": "missing"}


def test_empty_list_and_404s(client):
    assert client.get("/api/apps/sessions").json() == []
    assert client.get("/api/apps/sessions/ghost").status_code == 404
    assert client.delete("/api/apps/sessions/ghost").status_code == 404
    assert client.get("/api/apps/sessions/ghost/logs").status_code == 404
    assert client.get("/api/apps/sessions/ghost/screenshot").status_code == 404
    assert client.get("/api/apps/sessions/ghost/thumb").status_code == 404


def test_deps_report_shape(client):
    report = client.get("/api/apps/deps").json()
    assert set(report) == {"ok", "checks", "install"}
    names = {check["name"] for check in report["checks"]}
    assert {"Xvfb", "xdotool", "webrtcbin"} <= names


def test_player_page(client):
    html = client.get("/apps/Out Of Body/play").text
    assert 'data-id="out-of-body"' in html
    assert "/static/app/client.js" in html
