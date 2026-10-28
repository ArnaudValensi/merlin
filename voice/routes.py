"""Voice mode API, mounted by the framework at ``/api/voice`` behind auth.

The page never learns the TTS URL or key: it asks Merlin, and Merlin talks to
the voice server.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import tts

URL_SLUG = "voice"

api_router = APIRouter()


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


@api_router.get("/status")
async def api_status():
    return capability()
