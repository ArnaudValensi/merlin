"""Apps page, player page, REST API, and the WebRTC signaling WebSocket.

The WebSocket only carries signaling (the SDP offer and answer, ICE
candidates) plus a few status events, so it works unchanged through the
merlincloud.dev proxy. The video and the input travel over WebRTC, peer to
peer when a path exists (the LAN, UPnP, STUN), else through Merlin Cloud's
relay (TURN). One viewer per app: a new viewer replaces the previous one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from auth import verify_ws_cookie
from merlin_ext import make_templates

from . import deps, iceservers, sessions

logger = logging.getLogger("merlin.ext.app")

APP_DIR = Path(__file__).parent.resolve()
STREAMER = APP_DIR / "streamer.py"
templates = make_templates(APP_DIR / "templates")

api_router = APIRouter()
page_router = APIRouter()

HELLO_TIMEOUT = 15.0
SERVERS_WAIT = 4.0  # for the portal's TURN credentials, then STUN alone
_ice_cache = iceservers.Cache()
RECORD_POLL = 0.5
FPS = 60


def bitrate_kbps(width: int, height: int) -> int:
    """8 Mbit/s at 1920x1080, proportional to the pixel count, clamped."""
    kbps = 8000 * width * height // (1920 * 1080)
    return max(1500, min(12000, kbps))


def _record_or_404(session_id: str) -> dict:
    try:
        return sessions.get(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No app named '{session_id}'")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@page_router.get("", response_class=HTMLResponse)
def apps_page(request: Request):
    return templates.TemplateResponse(
        request, "apps.html", {"home_dir": str(Path.home())}
    )


@page_router.get("/{session_id}/play", response_class=HTMLResponse)
def player_page(request: Request, session_id: str):
    return templates.TemplateResponse(
        request, "player.html", {"session_id": sessions.slugify(session_id)}
    )


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@api_router.get("/sessions")
async def list_sessions():
    records = await asyncio.to_thread(sessions.list_sessions)
    return [sessions.public(r) for r in records]


@api_router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    return sessions.public(await asyncio.to_thread(_record_or_404, session_id))


@api_router.delete("/sessions/{session_id}")
async def stop_session(session_id: str):
    try:
        record = await asyncio.to_thread(sessions.stop, session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No app named '{session_id}'")
    await _end_viewer(session_id, {"type": "exited", "reason": "stopped"})
    return sessions.public(record)


@api_router.get("/sessions/{session_id}/logs", response_class=PlainTextResponse)
async def session_logs(session_id: str, tail: int = 200):
    await asyncio.to_thread(_record_or_404, session_id)
    return await asyncio.to_thread(sessions.read_log, session_id, tail)


@api_router.get("/sessions/{session_id}/screenshot")
async def session_screenshot(session_id: str):
    try:
        path = await asyncio.to_thread(sessions.screenshot, session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No app named '{session_id}'")
    except sessions.AppError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return FileResponse(
        path, media_type="image/png", filename=f"{session_id}-screenshot.png"
    )


@api_router.get("/sessions/{session_id}/thumb")
async def session_thumb(session_id: str):
    path = sessions.thumb_path(sessions.slugify(session_id))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No thumbnail yet")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "no-cache"}
    )


@api_router.post("/sessions")
async def launch_session(request: Request):
    """Launch a saved app (``saved_id``) or a command (``argv``, ``cwd``)."""
    body = await request.json()
    size = None
    if body.get("size"):
        try:
            size = sessions.parse_size(str(body["size"]))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    try:
        if body.get("saved_id"):
            record = await asyncio.to_thread(
                sessions.launch_saved, str(body["saved_id"]), size
            )
        else:
            argv = body.get("argv")
            if not isinstance(argv, list) or not argv:
                raise HTTPException(status_code=400, detail="saved_id or argv required")
            record = await asyncio.to_thread(
                lambda: sessions.launch(
                    [str(a) for a in argv],
                    name=body.get("name"),
                    cwd=body.get("cwd"),
                    size=size or sessions.DEFAULT_SIZE,
                    gpu=body.get("gpu") or "auto",
                    controls=body.get("controls"),
                    keys=body.get("keys") or {},
                    origin={"kind": "dashboard"},
                    audio=body.get("audio") or "stream",
                )
            )
    except KeyError:
        raise HTTPException(status_code=404, detail="No saved app with that id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except sessions.AppError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    out = sessions.public(record)
    if record["status"] == "exited":
        out["log_tail"] = await asyncio.to_thread(sessions.read_log, record["id"], 20)
    return out


@api_router.get("/saved")
async def list_saved():
    return await asyncio.to_thread(sessions.list_saved)


@api_router.post("/saved")
async def create_saved(request: Request):
    try:
        return await asyncio.to_thread(sessions.save_saved, await request.json())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@api_router.put("/saved/{saved_id}")
async def update_saved(saved_id: str, request: Request):
    try:
        return await asyncio.to_thread(
            sessions.save_saved, await request.json(), saved_id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="No saved app with that id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@api_router.delete("/saved/{saved_id}")
async def delete_saved(saved_id: str):
    try:
        await asyncio.to_thread(sessions.delete_saved, saved_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No saved app with that id")
    return {"ok": True}


@api_router.get("/deps")
async def prerequisites():
    return await asyncio.to_thread(deps.report)


# ---------------------------------------------------------------------------
# Signaling
# ---------------------------------------------------------------------------


@dataclass
class Viewer:
    websocket: WebSocket
    process: asyncio.subprocess.Process | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    generation: str | None = None  # the app launch this viewer streams

    async def send(self, message: dict) -> None:
        if self.websocket.application_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await self.websocket.send_text(json.dumps(message))


_viewers: dict[str, Viewer] = {}
# Viewer replacement and streamer start-up are serialized per app, so two
# connections arriving together still end with exactly one viewer: the last.
_viewer_locks: dict[str, asyncio.Lock] = {}


async def _end_viewer(session_id: str, message: dict) -> None:
    viewer = _viewers.get(session_id)
    if viewer is not None:
        await viewer.send(message)
        viewer.done.set()


def _signal_group(process: asyncio.subprocess.Process, sig: int) -> None:
    """Signal the streamer's own process group: it and an `xdotool type` it
    may have started. Only while it is not reaped yet (``returncode`` None):
    until then its PID, hence the group number, cannot be reused."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sig)


async def _kill(process: asyncio.subprocess.Process | None) -> None:
    """Stop a streamer gracefully: closing its stdin makes it stop typing,
    release any key or button the viewer held, and quit. Only if it does not
    is its whole group signalled (a blocked streamer, a long paste)."""
    if process is None or process.returncode is not None:
        return
    if process.stdin is not None:
        with contextlib.suppress(Exception):
            process.stdin.close()
    for sig, grace in ((None, 3.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 5.0)):
        if sig is not None:
            _signal_group(process, sig)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
            return
        except TimeoutError:
            continue


async def _pump_streamer_out(viewer: Viewer) -> None:
    assert viewer.process is not None and viewer.process.stdout is not None
    async for line in viewer.process.stdout:
        text = line.decode(errors="replace").strip()
        if text:
            with contextlib.suppress(Exception):
                await viewer.websocket.send_text(text)
    await viewer.send({"type": "error", "message": "The streamer stopped."})


async def _pump_streamer_err(viewer: Viewer, session_id: str) -> None:
    assert viewer.process is not None and viewer.process.stderr is not None
    async for line in viewer.process.stderr:
        logger.info("[%s] %s", session_id, line.decode(errors="replace").rstrip())


async def _pump_browser(viewer: Viewer) -> None:
    assert viewer.process is not None and viewer.process.stdin is not None
    while True:
        text = await viewer.websocket.receive_text()
        try:
            message = json.loads(text)
        except ValueError:
            continue
        if message.get("type") in ("answer", "ice"):
            viewer.process.stdin.write((json.dumps(message) + "\n").encode())
            await viewer.process.stdin.drain()


async def _watch_record(viewer: Viewer, session_id: str, last_input: str | None):
    while True:
        await asyncio.sleep(RECORD_POLL)
        try:
            record = await asyncio.to_thread(sessions.get, session_id)
        except KeyError:
            await viewer.send({"type": "exited", "reason": "stopped"})
            return
        if record.get("generation") != viewer.generation:
            # Relaunched (run --replace): this stream shows a dead display;
            # the client reconnects to the new launch.
            await viewer.send({"type": "restarted"})
            return
        if record["status"] == "exited":
            await viewer.send(
                {"type": "exited", "reason": "exited", "code": record["exit_code"]}
            )
            return
        if record.get("last_agent_input_at") != last_input:
            last_input = record.get("last_agent_input_at")
            await viewer.send({"type": "agent_input", "at": last_input})


async def stream_ws(websocket: WebSocket, session_id: str) -> None:
    if not verify_ws_cookie(websocket):
        await websocket.close(code=4401, reason="Unauthorized")
        return
    await websocket.accept()
    viewer = Viewer(websocket)

    try:
        record = await asyncio.to_thread(sessions.get, session_id)
    except KeyError:
        await viewer.send({"type": "exited", "reason": "missing"})
        await websocket.close()
        return
    if record["status"] not in ("starting", "running"):
        await viewer.send(
            {"type": "exited", "reason": "exited", "code": record["exit_code"]}
        )
        await websocket.close()
        return

    await viewer.send(
        {
            "type": "welcome",
            "host": socket.gethostname(),
            "app": sessions.public(record),
        }
    )
    # The ICE servers (TURN credentials from the portal, cached) while the
    # browser says hello.
    servers_task = asyncio.create_task(asyncio.to_thread(_ice_cache.get))
    try:
        hello = json.loads(
            await asyncio.wait_for(websocket.receive_text(), HELLO_TIMEOUT)
        )
    except (TimeoutError, WebSocketDisconnect, ValueError):
        servers_task.cancel()
        with contextlib.suppress(Exception):
            await websocket.close()
        return
    if not isinstance(hello, dict):
        hello = {}
    codecs = [str(c) for c in hello.get("codecs") or ["VP8"]]
    try:
        servers = await asyncio.wait_for(asyncio.shield(servers_task), SERVERS_WAIT)
    except Exception:  # a slow or failing portal: STUN alone this time
        servers = iceservers.Servers(
            iceservers.stun_servers(), iceservers.TURN_UNREACHABLE
        )
    # ?ice=relay on the page: the browser goes through the relay only (a
    # test switch, for TURN on purpose). The streamer keeps every path: a
    # relay reaches any address, but two relays of the same server cannot
    # reach each other (the relay refuses its own address as a peer).
    policy = "relay" if hello.get("ice") == "relay" else "all"
    ice = servers.to_json()

    async with _viewer_locks.setdefault(session_id, asyncio.Lock()):
        if websocket.application_state != WebSocketState.CONNECTED:
            return
        previous = _viewers.get(session_id)
        if previous is not None:
            await previous.send({"type": "replaced"})
            previous.done.set()
            await _kill(previous.process)
        # Re-read right before attaching: during the handshake the app may
        # have been stopped or relaunched on another display.
        try:
            record = await asyncio.to_thread(sessions.get, session_id)
        except KeyError:
            await viewer.send({"type": "exited", "reason": "stopped"})
            return
        if record["status"] not in ("starting", "running"):
            await viewer.send(
                {"type": "exited", "reason": "exited", "code": record["exit_code"]}
            )
            return
        viewer.generation = record.get("generation")
        width, height = record["size"]
        _viewers[session_id] = viewer
        # Before the streamer exists, so before its offer: the browser builds
        # its peer with them. To the streamer through its environment, never
        # its argv (the credentials would show in ps).
        await viewer.send({"type": "servers", **ice, "policy": policy})
        viewer.process = await asyncio.create_subprocess_exec(
            deps.SYSTEM_PYTHON,
            str(STREAMER),
            "--display",
            record["display"],
            "--codecs",
            ",".join(codecs),
            "--fps",
            str(FPS),
            "--bitrate",
            str(bitrate_kbps(width, height)),
            "--xvfb-pid",
            str(record.get("xvfb_pid") or 0),
            "--xvfb-start",
            str(record.get("xvfb_start") or 0),
            "--state-lock",
            str(sessions.apps_dir() / ".lock"),
            "--keymap-registry",
            str(sessions.keymap_registry(record)),
            "--audio-sink",
            record.get("audio_sink") or "",
            "--app",
            str(record.get("id") or session_id),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={**os.environ, "MERLIN_APP_ICE": json.dumps(ice)},
        )
    logger.info(
        "viewer connected to %s (streamer pid %s)", session_id, viewer.process.pid
    )
    tasks = [
        asyncio.create_task(_pump_streamer_out(viewer)),
        asyncio.create_task(_pump_browser(viewer)),
        asyncio.create_task(
            _watch_record(viewer, session_id, record.get("last_agent_input_at"))
        ),
        asyncio.create_task(viewer.done.wait()),
    ]
    stderr_task = asyncio.create_task(_pump_streamer_err(viewer, session_id))
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await _kill(viewer.process)
        stderr_task.cancel()
        if _viewers.get(session_id) is viewer:
            del _viewers[session_id]
            await asyncio.to_thread(sessions.capture_thumbnail, session_id)
        if websocket.application_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()
        logger.info("viewer left %s", session_id)


def register_routes(app: FastAPI) -> None:
    """The signaling WebSocket. Auth like the terminal's: cookie or portal."""
    app.add_api_websocket_route("/ws/apps/{session_id}/stream", stream_ws)


async def start() -> None:
    """Sweep sessions whose app died while Merlin was down."""
    await asyncio.to_thread(sessions.sweep)
