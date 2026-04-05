"""Transcribe audio files using faster-whisper or OpenAI Whisper API.

Three backends, selected by environment variables:
  1. MERLIN_SAAS_TOKEN set → SaaS proxy (Merlin Cloud)
  2. OPENAI_API_KEY set    → OpenAI Whisper API (cloud, fast)
  3. Neither               → local faster-whisper (offline, slower)

Examples:
    # Transcribe using local model (default)
    uv run transcribe.py recording.ogg

    # Transcribe using OpenAI API
    OPENAI_API_KEY=sk-... uv run transcribe.py recording.ogg
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("merlin.transcribe")

MODEL_SIZE = "medium"
COMPUTE_TYPE = "int8"  # Optimised for CPU
LANGUAGE = "fr"

_backend_logged = False


# ---------------------------------------------------------------------------
# Backend: local (faster-whisper)
# ---------------------------------------------------------------------------

_model = None  # lazy-loaded WhisperModel singleton


def _get_model():
    """Lazy-load the Whisper model (singleton)."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL_SIZE, compute_type=COMPUTE_TYPE)
    return _model


def _transcribe_local(audio_path: str | Path, language: str) -> str:
    """Transcribe using local faster-whisper model."""
    model = _get_model()
    segments, _info = model.transcribe(str(audio_path), language=language)
    return " ".join(seg.text.strip() for seg in segments).strip()


# ---------------------------------------------------------------------------
# Backend: OpenAI Whisper API
# ---------------------------------------------------------------------------


def _transcribe_openai(audio_path: str | Path, language: str, api_key: str) -> str:
    """Transcribe using the OpenAI Whisper API."""
    import httpx

    path = Path(audio_path)
    with open(path, "rb") as f:
        response = httpx.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (path.name, f, "application/octet-stream")},
            data={"model": "whisper-1", "language": language},
            timeout=30.0,
        )
    response.raise_for_status()
    return response.json()["text"]


# ---------------------------------------------------------------------------
# Backend: SaaS proxy (Merlin Cloud)
# ---------------------------------------------------------------------------


MERLIN_CLOUD_API = "https://merlincloud.dev"


def _transcribe_saas(audio_path: str | Path, language: str, token: str) -> str:
    """Transcribe via the Merlin Cloud SaaS proxy."""
    import httpx

    api = os.getenv("MERLIN_SAAS_API", MERLIN_CLOUD_API)
    path = Path(audio_path)
    with open(path, "rb") as f:
        response = httpx.post(
            f"{api.rstrip('/')}/api/transcribe",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (path.name, f, "application/octet-stream")},
            data={"language": language},
            timeout=60.0,
        )
    response.raise_for_status()
    return response.json()["text"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transcribe(audio_path: str | Path, language: str = LANGUAGE) -> str:
    """Transcribe an audio file and return the text.

    Backend is selected by environment variables:
      - MERLIN_SAAS_TOKEN → SaaS proxy (Merlin Cloud)
      - OPENAI_API_KEY    → OpenAI Whisper API
      - Neither           → local faster-whisper

    Args:
        audio_path: Path to an audio file (any format supported by the backend).
        language: Language code (e.g. "en", "fr"). Defaults to LANGUAGE constant.

    Returns:
        The transcribed text.
    """
    global _backend_logged

    if saas_token := os.getenv("MERLIN_SAAS_TOKEN"):
        if not _backend_logged:
            logger.info("Transcription backend: SaaS proxy")
            _backend_logged = True
        return _transcribe_saas(audio_path, language, saas_token)
    elif api_key := os.getenv("OPENAI_API_KEY"):
        if not _backend_logged:
            logger.info("Transcription backend: OpenAI Whisper API")
            _backend_logged = True
        return _transcribe_openai(audio_path, language, api_key)
    else:
        if not _backend_logged:
            logger.info("Transcription backend: local (faster-whisper)")
            _backend_logged = True
        return _transcribe_local(audio_path, language)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe audio files using faster-whisper or OpenAI Whisper API.",
        epilog=(
            "Backend selection (by environment variable):\n"
            "  MERLIN_SAAS_TOKEN → SaaS proxy (merlincloud.dev)\n"
            "  OPENAI_API_KEY    → OpenAI Whisper API (cloud)\n"
            "  Neither           → local faster-whisper (default)\n"
            "\n"
            "Examples:\n"
            "  uv run transcribe.py voice-message.ogg\n"
            "  OPENAI_API_KEY=sk-... uv run transcribe.py recording.ogg\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("audio_file", help="Path to the audio file to transcribe")
    args = parser.parse_args()

    path = Path(args.audio_file)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    logging.basicConfig(level=logging.INFO)
    text = transcribe(path)
    print(text)


if __name__ == "__main__":
    main()
