"""Voice mode API, mounted by the framework at ``/api/voice`` behind auth.

The page never learns the TTS URL or key: it asks Merlin, and Merlin talks to
the voice server. Audio is fetched over HTTP by id, never pushed over the
terminal socket. See ``docs/dev/voice-mode.md``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from board import sweep

from . import session, tts

URL_SLUG = "voice"
# The console and the vendored VAD, at /static/voice.
STATIC_DIR = Path(__file__).parent / "static"

api_router = APIRouter()

LANGS = ("en", "fr")


def capability() -> dict:
    """Whether voice mode can run at all, and why not when it cannot."""
    config = tts.tts_config()
    if config is None:
        return {
            "available": False,
            "reason": "No voice server configured (merlin config tts-url <url>)",
        }
    return {
        "available": True,
        "reason": None,
        "voices": {"en": config.voice_en, "fr": config.voice_fr},
    }


class Target(BaseModel):
    target: str


class StartBody(Target):
    lang: str = "en"


class AnswerBody(Target):
    yes: bool


class SkipBody(Target):
    turn: int | None = None


def _lang(value: str) -> str:
    value = (value or "").strip().lower()[:2]
    return value if value in LANGS else "en"


def _bad_target() -> JSONResponse:
    return JSONResponse({"error": "Not a terminal window"}, status_code=400)


def _no_session() -> JSONResponse:
    return JSONResponse(
        {"error": "Voice mode is not on for this window"}, status_code=404
    )


@api_router.get("/status")
async def api_status():
    return capability()


@api_router.get("/check")
async def api_check(target: str):
    """Whether voice mode can start on this window, and why not."""
    try:
        await session.preflight(target)
    except session.StartRefused as e:
        return {"available": False, "reason": str(e), "target": target}
    return {"available": True, "reason": None, "target": target}


@api_router.post("/start")
async def api_start(body: StartBody):
    try:
        running = await session.start_session(body.target, _lang(body.lang))
    except session.StartRefused as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return running.snapshot()


@api_router.post("/stop")
async def api_stop(body: Target):
    if not session.valid_target(body.target):
        return _bad_target()
    await session.stop_session(body.target, "Voice mode turned off")
    return {"ok": True}


@api_router.get("/session")
async def api_session(target: str):
    """The session's state, for a page that just (re)connected."""
    if not session.valid_target(target):
        return _bad_target()
    running = session.get(target)
    if running is None:
        return {"active": False, "target": target}
    return running.snapshot()


@api_router.post("/lang")
async def api_lang(body: StartBody):
    running = session.get(body.target)
    if running is None:
        return _no_session()
    running.set_lang(_lang(body.lang))
    return {"ok": True, "lang": running.lang}


@api_router.post("/skip")
async def api_skip(body: SkipBody):
    """Drop the rest of the answer being spoken: ``turn``, the one the page
    is playing."""
    running = session.get(body.target)
    if running is None:
        return _no_session()
    await running.skip(body.turn)
    return {"ok": True}


async def _escape(target: str) -> bool:
    """Escape to the window, and the idle write that the tmux Escape binding
    would have done (``send-keys`` bypasses key bindings)."""
    if not await asyncio.to_thread(sweep.send_escape, target):
        return False
    state = await asyncio.to_thread(sweep.window_options, target, ("@agent_state",))
    if state and state["@agent_state"] in ("busy", "ask"):
        await asyncio.to_thread(sweep.set_window_option, target, "@agent_state", "idle")
    return True


@api_router.post("/answer")
async def api_answer(body: AnswerBody):
    """Yes (Enter) or No (Escape) to the dialog the agent is blocked on."""
    running = session.get(body.target)
    if running is None:
        return _no_session()
    if body.yes:
        ok = await asyncio.to_thread(sweep.send_enter, body.target)
    else:
        ok = await _escape(body.target)
    if not ok:
        return JSONResponse(
            {"error": "The window did not take the key"}, status_code=502
        )
    return {"ok": True}


@api_router.post("/interrupt")
async def api_interrupt(body: Target):
    """Stop the agent (Escape), and drop every answer waiting to be spoken."""
    running = session.get(body.target)
    if running is None:
        return _no_session()
    await running.silence()
    if not await _escape(body.target):
        return JSONResponse(
            {"error": "The window did not take the key"}, status_code=502
        )
    return {"ok": True}


@api_router.get("/audio/{clip_id}")
async def api_audio(clip_id: str):
    found = session.find_clip(clip_id)
    if found is None:
        return JSONResponse({"error": "No such clip"}, status_code=404)
    data, content_type = found
    return Response(
        data, media_type=content_type, headers={"Cache-Control": "no-store"}
    )
