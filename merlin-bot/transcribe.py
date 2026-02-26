# /// script
# dependencies = [
#   "faster-whisper",
# ]
# ///
"""Transcribe audio files using faster-whisper.

Uses the 'base' model with language forced to French. Claude handles
translation/understanding since it's bilingual.

Examples:
    # Transcribe a local file
    uv run transcribe.py recording.ogg

    # Transcribe and see just the text
    uv run transcribe.py voice-message.ogg
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from faster_whisper import WhisperModel

# Model is downloaded on first use (~150MB for 'base') and cached in
# ~/.cache/huggingface/.  Subsequent calls are fast.
_model: WhisperModel | None = None

MODEL_SIZE = "medium"
COMPUTE_TYPE = "int8"  # Optimised for CPU
LANGUAGE = "fr"


def _get_model() -> WhisperModel:
    """Lazy-load the Whisper model (singleton)."""
    global _model
    if _model is None:
        _model = WhisperModel(MODEL_SIZE, compute_type=COMPUTE_TYPE)
    return _model


def transcribe(audio_path: str | Path, language: str = LANGUAGE) -> str:
    """Transcribe an audio file and return the text.

    Args:
        audio_path: Path to an audio file (any format ffmpeg can decode).
        language: Language code (e.g. "en", "fr"). Defaults to LANGUAGE constant.

    Returns:
        The transcribed text.
    """
    model = _get_model()
    segments, _info = model.transcribe(str(audio_path), language=language)
    return " ".join(seg.text.strip() for seg in segments).strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe audio files using faster-whisper.",
        epilog=(
            "The 'base' model is downloaded on first run (~150MB).\n"
            "Language is forced to French — Claude handles understanding.\n"
            "\n"
            "Examples:\n"
            "  uv run transcribe.py voice-message.ogg\n"
            "  uv run transcribe.py /tmp/audio.wav\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("audio_file", help="Path to the audio file to transcribe")
    args = parser.parse_args()

    path = Path(args.audio_file)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    text = transcribe(path)
    print(text)


if __name__ == "__main__":
    main()
