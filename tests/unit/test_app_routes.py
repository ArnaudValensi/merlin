"""Apps REST routes, pages and the signaling socket's auth, on a bare app."""

import time

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


class _FakeStdin:
    def __init__(self, proc):
        self.proc = proc

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.proc.returncode = 0  # a graceful streamer quits on stdin EOF


class _Silent:
    """A stdout/stderr that never produces a line (a streamer still running)."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio

        await asyncio.sleep(3600)
        raise StopAsyncIteration


class _FakeProcess:
    pid = 999999

    def __init__(self):
        self.returncode = None
        self.stdin = _FakeStdin(self)
        self.stdout = _Silent()
        self.stderr = _Silent()

    async def wait(self):
        return self.returncode


def _record(generation, display, xvfb_pid):
    return {
        "id": "probe",
        "name": "probe",
        "generation": generation,
        "display": display,
        "size": [1280, 720],
        "status": "running",
        "exit_code": None,
        "xvfb_pid": xvfb_pid,
        "xvfb_start": 1,
        "last_agent_input_at": None,
    }


def test_viewer_attaches_to_the_launch_current_at_attach_time(client, monkeypatch):
    """The app is relaunched on another display between welcome and hello:
    the streamer must attach to the new display, and a later relaunch tells
    the client to reconnect."""
    import asyncio

    from app import sessions

    current = {"record": _record("first", ":100", 111)}
    monkeypatch.setattr(sessions, "get", lambda _id: dict(current["record"]))
    monkeypatch.setattr(sessions, "capture_thumbnail", lambda _id: None)
    spawned = []

    async def fake_exec(*argv, **kwargs):
        spawned.append((argv, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with client.websocket_connect("/ws/apps/probe/stream") as ws:
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
        current["record"] = _record("second", ":101", 222)  # relaunched meanwhile
        ws.send_json({"type": "hello", "codecs": ["H264"]})
        deadline = time.monotonic() + 5
        while not spawned:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        argv, kwargs = spawned[0]
        assert argv[argv.index("--display") + 1] == ":101"
        assert argv[argv.index("--xvfb-pid") + 1] == "222"
        assert kwargs.get("start_new_session") is True

        assert ws.receive_json()["type"] == "servers"
        current["record"] = _record("third", ":102", 333)
        assert ws.receive_json() == {"type": "restarted"}


def _spawn_one(client, monkeypatch, hello):
    """Connect, say hello, and return (messages before the spawn, argv, env)."""
    import asyncio

    from app import sessions

    monkeypatch.setattr(sessions, "get", lambda _id: _record("g", ":100", 111))
    monkeypatch.setattr(sessions, "capture_thumbnail", lambda _id: None)
    spawned = []

    async def fake_exec(*argv, **kwargs):
        spawned.append((argv, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with client.websocket_connect("/ws/apps/probe/stream") as ws:
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json(hello)
        servers = ws.receive_json()
        deadline = time.monotonic() + 5
        while not spawned:
            assert time.monotonic() < deadline
            time.sleep(0.02)
    argv, kwargs = spawned[0]
    return servers, argv, kwargs.get("env") or {}


TURN = {
    "urls": ["turn:turn.merlincloud.dev:3478?transport=udp"],
    "username": "1790900000:lisa",
    "credential": "secret-credential",
}


def test_the_browser_and_the_streamer_get_the_same_servers(client, monkeypatch):
    import json

    from app import iceservers

    servers = iceservers.Servers([{"urls": ["stun:s:3478"]}, TURN], "ok", 9e12)
    monkeypatch.setattr(routes._ice_cache, "get", lambda: servers)
    message, argv, env = _spawn_one(client, monkeypatch, {"type": "hello"})
    assert message == {
        "type": "servers",
        "iceServers": [{"urls": ["stun:s:3478"]}, TURN],
        "turn": "ok",
        "policy": "all",
        "mode": "auto",
    }
    assert json.loads(env["MERLIN_APP_ICE"]) == {
        "iceServers": [{"urls": ["stun:s:3478"]}, TURN],
        "turn": "ok",
        "upnp": True,
        "mode": "auto",
    }
    assert not any("secret-credential" in str(a) for a in argv)  # never in ps


def test_ice_relay_in_hello_sends_the_browser_through_the_relay(client, monkeypatch):
    """Only the browser: two relays of the same server cannot reach each
    other (it refuses its own address as a peer)."""
    message, _argv, env = _spawn_one(
        client, monkeypatch, {"type": "hello", "ice": "relay"}
    )
    assert message["policy"] == "relay"
    assert message["mode"] == "relay"
    assert "policy" not in env["MERLIN_APP_ICE"]


def test_a_slow_portal_does_not_hold_the_stream(client, monkeypatch):
    from app import iceservers

    monkeypatch.setattr(routes, "SERVERS_WAIT", 0.2)
    monkeypatch.setattr(routes._ice_cache, "get", lambda: time.sleep(3))
    started = time.monotonic()
    message, _argv, _env = _spawn_one(client, monkeypatch, {"type": "hello"})
    assert time.monotonic() - started < 2.5
    assert message["iceServers"] == iceservers.stun_servers()
    assert message["turn"] == iceservers.TURN_UNREACHABLE


def test_without_merlin_cloud_the_stream_gets_stun_only(client, monkeypatch):
    from app import iceservers

    monkeypatch.delenv("MERLIN_APP_STUN", raising=False)
    message, _argv, _env = _spawn_one(client, monkeypatch, {"type": "hello"})
    assert message["iceServers"] == [{"urls": [iceservers.DEFAULT_STUN]}]
    assert message["turn"] == "off"


class _Lines:
    """A streamer's stdout: these lines, then it keeps running."""

    def __init__(self, *lines):
        self.lines = [line.encode() + b"\n" for line in lines]

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio

        if self.lines:
            return self.lines.pop(0)
        await asyncio.sleep(3600)
        raise StopAsyncIteration


def test_a_stopped_app_is_said_stopped_not_a_streamer_error(client, monkeypatch):
    """Stopping kills the display under the streamer, which reports an
    error: the viewer must hear "stopped", not retry into "no app named"."""
    import asyncio

    from app import sessions

    record = {"r": _record("g", ":100", 111)}

    def get(_id):
        if record["r"] is None:
            raise KeyError(_id)
        return dict(record["r"])

    monkeypatch.setattr(sessions, "get", get)
    monkeypatch.setattr(sessions, "capture_thumbnail", lambda _id: None)

    async def fake_exec(*argv, **kwargs):
        process = _FakeProcess()
        record["r"] = None  # stopped while the streamer starts
        process.stdout = _Lines('{"type": "error", "message": "X server gone"}')
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with client.websocket_connect("/ws/apps/probe/stream") as ws:
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "hello"})
        assert ws.receive_json()["type"] == "servers"
        assert ws.receive_json() == {"type": "exited", "reason": "stopped"}


def test_a_streamer_error_while_the_app_runs_goes_through(client, monkeypatch):
    import asyncio

    from app import sessions

    monkeypatch.setattr(sessions, "get", lambda _id: _record("g", ":100", 111))
    monkeypatch.setattr(sessions, "capture_thumbnail", lambda _id: None)

    async def fake_exec(*argv, **kwargs):
        process = _FakeProcess()
        process.stdout = _Lines('{"type": "error", "message": "encoder failed"}')
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with client.websocket_connect("/ws/apps/probe/stream") as ws:
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "hello"})
        assert ws.receive_json()["type"] == "servers"
        assert ws.receive_json() == {"type": "error", "message": "encoder failed"}


def test_a_test_mode_reaches_both_ends(client, monkeypatch):
    import json

    from app import iceservers

    servers = iceservers.Servers([{"urls": ["stun:s:3478"]}, TURN], "ok", 9e12)
    monkeypatch.setattr(routes._ice_cache, "get", lambda: servers)
    message, _argv, env = _spawn_one(
        client, monkeypatch, {"type": "hello", "ice": "stun"}
    )
    assert message["mode"] == "stun"
    assert message["iceServers"] == [{"urls": ["stun:s:3478"]}]  # no relay
    streamer = json.loads(env["MERLIN_APP_ICE"])
    assert streamer["upnp"] is False and streamer["iceServers"] == message["iceServers"]
    message, _argv, env = _spawn_one(
        client, monkeypatch, {"type": "hello", "ice": ["not", "a", "mode"]}
    )
    assert message["mode"] == "auto"
