"""Voice mode: a hands-free conversation with the agent of a terminal window.

The page listens and uploads each utterance through dictation, the agent
answers in a style written for the ear, and Merlin reads the answer aloud
through one OpenAI-compatible text-to-speech endpoint (``voice/tts.py``).
"""

from .routes import STATIC_DIR, URL_SLUG, api_router

__all__ = ["STATIC_DIR", "URL_SLUG", "api_router"]
