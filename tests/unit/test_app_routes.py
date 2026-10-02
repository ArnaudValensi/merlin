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


def test_saved_apps_crud(client, tmp_path):
    body = {
        "name": "Out of Body",
        "command": "./build/OutOfBody --x 'a b'",
        "cwd": str(tmp_path),
        "controls": "gamepad",
        "keys": "A=x,B=z",
        "gpu": "auto",
        "size": "fit",
    }
    created = client.post("/api/apps/saved", json=body).json()
    assert created["id"] == "out-of-body"
    assert created["keys"] == {"A": "x", "B": "z"}
    assert client.get("/api/apps/saved").json() == [created]
    assert client.post("/api/apps/saved", json=body).status_code == 400  # duplicate

    body["size"] = "1281x721"
    updated = client.put("/api/apps/saved/out-of-body", json=body).json()
    assert updated["size"] == "1280x720"
    assert client.put("/api/apps/saved/ghost", json=body).status_code == 404

    assert client.delete("/api/apps/saved/out-of-body").json() == {"ok": True}
    assert client.get("/api/apps/saved").json() == []
    assert client.delete("/api/apps/saved/out-of-body").status_code == 404


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"name": ""}, "name is required"),
        ({"command": ""}, "command is required"),
        ({"command": "'unclosed"}, "command"),
        ({"cwd": "/nope/nowhere"}, "folder not found"),
        ({"controls": "joystick"}, "controls"),
        ({"gpu": "maybe"}, "gpu"),
        ({"keys": "A"}, "key mapping"),
        ({"size": "huge"}, "size"),
    ],
)
def test_saved_app_validation(client, tmp_path, patch, message):
    body = {"name": "x", "command": "true", "cwd": str(tmp_path)}
    body.update(patch)
    response = client.post("/api/apps/saved", json=body)
    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_launch_needs_saved_id_or_argv(client):
    assert client.post("/api/apps/sessions", json={}).status_code == 400
    assert (
        client.post("/api/apps/sessions", json={"saved_id": "ghost"}).status_code == 404
    )
    assert (
        client.post(
            "/api/apps/sessions", json={"argv": ["true"], "size": "big"}
        ).status_code
        == 400
    )


def test_missing_tools_are_named_with_their_package(monkeypatch):
    import shutil as real_shutil

    from app import deps, sessions

    real_which = real_shutil.which

    def which(name, *args, **kwargs):
        return None if name == "Xvfb" else real_which(name, *args, **kwargs)

    monkeypatch.setattr(deps.shutil, "which", which)
    message = deps.missing_message(["Xvfb", "sh"])
    assert message.startswith("Missing Xvfb (package: xorg-server-xvfb)")
    report = deps.report()
    assert report["ok"] is False
    [xvfb] = [c for c in report["checks"] if c["name"] == "Xvfb"]
    assert xvfb == {
        "name": "Xvfb",
        "ok": False,
        "required": True,
        "hint": "xorg-server-xvfb",
    }
    monkeypatch.setattr(sessions.shutil, "which", which)
    with pytest.raises(sessions.AppError, match="Missing Xvfb"):
        sessions.launch(["true"], gpu="off")
