#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = [
#     "numpy",
#     "kokoro-onnx>=0.4",
#     "onnxruntime-gpu[cuda,cudnn]>=1.22",
# ]
# [tool.uv]
# # kokoro-onnx depends on the CPU onnxruntime, which would shadow the GPU build.
# override-dependencies = ["onnxruntime ; sys_platform == 'never'"]
# ///
"""Kokoro text-to-speech behind an OpenAI-compatible HTTP API.

The reference voice server for Merlin's voice mode. It implements the one
endpoint Merlin speaks, ``POST /v1/audio/speech``, so pointing
``MERLIN_TTS_URL`` at ``http://127.0.0.1:8880/v1`` is all Merlin needs. Any
other server that speaks this API works the same way: Merlin never imports
this script.

The model loads once (CUDA first, CPU fallback), warms up, and requests are
synthesized one at a time. The output is plain Kokoro, no effect.
"""

from __future__ import annotations

import argparse
import hmac
import io
import json
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol

SAMPLE_RATE = 24000
MAX_INPUT_CHARS = 4096
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
MODELS_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0"

# Kokoro names its voices <language><gender>_<name>: the first letter is the
# language the phonemizer needs.
VOICE_LANGS = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "cmn",
}
FORMATS = ("wav", "pcm")


class Synth(Protocol):
    """What the HTTP layer needs: 16-bit mono PCM at SAMPLE_RATE for a text."""

    voices: list[str]
    provider: str

    def synthesize(self, text: str, voice: str, speed: float) -> bytes: ...


class KokoroSynth:
    """Kokoro loaded once on the GPU when it can, the CPU otherwise."""

    def __init__(self, models: Path) -> None:
        import numpy as np
        import onnxruntime as ort

        try:
            ort.preload_dlls()
        except AttributeError:  # older onnxruntime builds
            pass
        from kokoro_onnx import Kokoro

        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        session = ort.InferenceSession(
            str(models / MODEL_FILE),
            sess_options=opts,
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        self._np = np
        self._kokoro = Kokoro.from_session(session, str(models / VOICES_FILE))
        self.provider = session.get_providers()[0]
        self.voices = sorted(self._kokoro.get_voices())
        self._lock = threading.Lock()

    def synthesize(self, text: str, voice: str, speed: float) -> bytes:
        np = self._np
        with self._lock:  # one request at a time on the model
            audio, _ = self._kokoro.create(
                text, voice=voice, speed=speed, lang=voice_lang(voice)
            )
        clipped = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
        return (clipped * 32767).astype("<i2").tobytes()


def voice_lang(voice: str) -> str:
    return VOICE_LANGS.get(voice[:1], "en-us")


def wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class RequestError(Exception):
    def __init__(self, status: int, message: str, kind: str = "invalid_request_error"):
        super().__init__(message)
        self.status = status
        self.kind = kind


def parse_speech_request(body: bytes, voices: list[str]) -> tuple[str, str, float, str]:
    """Validate an OpenAI speech body. Returns (input, voice, speed, format)."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise RequestError(400, "body is not valid JSON") from None
    if not isinstance(data, dict):
        raise RequestError(400, "body must be a JSON object")
    text = data.get("input")
    if not isinstance(text, str) or not text.strip():
        raise RequestError(400, "'input' is required")
    if len(text) > MAX_INPUT_CHARS:
        raise RequestError(400, f"'input' is longer than {MAX_INPUT_CHARS} characters")
    voice = data.get("voice") or "af_heart"
    if not isinstance(voice, str) or voice not in voices:
        raise RequestError(400, f"unknown voice {voice!r}")
    fmt = data.get("response_format") or "wav"
    if fmt not in FORMATS:
        raise RequestError(
            400, f"response_format {fmt!r} is not supported (use wav or pcm)"
        )
    try:
        speed = float(data.get("speed", 1.0))
    except (TypeError, ValueError):
        raise RequestError(400, "'speed' must be a number") from None
    if not 0.25 <= speed <= 4.0:
        raise RequestError(400, "'speed' must be between 0.25 and 4.0")
    return text, voice, speed, fmt


def make_handler(synth: Synth, api_key: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "kokoro-tts-server"

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            sys.stderr.write(f"{time.strftime('%H:%M:%S')} {format % args}\n")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _error(self, err: RequestError) -> None:
            self._json(err.status, {"error": {"message": str(err), "type": err.kind}})

        def _authorized(self) -> bool:
            if not api_key:
                return True
            given = self.headers.get("Authorization", "")
            if hmac.compare_digest(given.encode(), f"Bearer {api_key}".encode()):
                return True
            self._error(RequestError(401, "invalid API key", "authentication_error"))
            return False

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/health":
                self._json(
                    200,
                    {
                        "status": "ok",
                        "provider": synth.provider,
                        "voices": len(synth.voices),
                    },
                )
                return
            if not self._authorized():
                return
            if path == "/v1/models":
                self._json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {"id": "kokoro", "object": "model", "owned_by": "kokoro"}
                        ],
                    },
                )
            elif path == "/v1/audio/voices":
                self._json(200, {"voices": synth.voices})
            else:
                self._error(RequestError(404, f"no route for GET {path}"))

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/")
            if not self._authorized():
                return
            if path != "/v1/audio/speech":
                self._error(RequestError(404, f"no route for POST {path}"))
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                text, voice, speed, fmt = parse_speech_request(
                    self.rfile.read(length), synth.voices
                )
            except RequestError as e:
                self._error(e)
                return
            started = time.monotonic()
            try:
                pcm = synth.synthesize(text, voice, speed)
            except Exception as e:  # the model failing is the server's fault
                self._error(RequestError(500, f"synthesis failed: {e}", "server_error"))
                return
            seconds = len(pcm) / 2 / SAMPLE_RATE
            sys.stderr.write(
                f"{time.strftime('%H:%M:%S')} {voice} {len(text)} chars -> "
                f"{seconds:.1f}s audio in {time.monotonic() - started:.2f}s\n"
            )
            if fmt == "pcm":
                self._send(200, pcm, "audio/pcm")
            else:
                self._send(200, wav_bytes(pcm), "audio/wav")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Endpoints:
  POST /v1/audio/speech   OpenAI body: input, voice, speed, response_format
                          (wav, or pcm for raw 16-bit mono {SAMPLE_RATE} Hz);
                          model is accepted and ignored
  GET  /v1/models         the one model, for discovery
  GET  /v1/audio/voices   the voice ids the model knows
  GET  /health            status, execution provider, voice count (no auth)

The language comes from the voice id: ff_siwis is French, af_heart American
English, bm_george British English, and so on.

Model files ({MODEL_FILE}, {VOICES_FILE}) come from
{MODELS_URL}

Examples:
  # Serve on the default port with the files in ~/.local/share/kokoro
  uv run --script tools/kokoro-tts-server.py

  # Use another models directory and require a key
  uv run --script tools/kokoro-tts-server.py \\
      --models ~/.local/share/nova/models --api-key s3cret

  # Point Merlin at it
  merlin config tts-url http://127.0.0.1:8880/v1

  # Try it
  curl -s localhost:8880/v1/audio/speech -H 'Content-Type: application/json' \\
      -d '{{"input": "Bonjour.", "voice": "ff_siwis"}}' -o bonjour.wav
""",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="bind address (default 127.0.0.1)"
    )
    parser.add_argument("--port", type=int, default=8880, help="port (default 8880)")
    parser.add_argument(
        "--models",
        type=Path,
        default=Path.home() / ".local" / "share" / "kokoro",
        help="directory holding the model files (default ~/.local/share/kokoro)",
    )
    parser.add_argument(
        "--api-key", help="require 'Authorization: Bearer <key>' on the API routes"
    )
    args = parser.parse_args()

    models = args.models.expanduser()
    missing = [f for f in (MODEL_FILE, VOICES_FILE) if not (models / f).is_file()]
    if missing:
        sys.exit(
            f"Missing model files in {models}: {', '.join(missing)}\n"
            f"Download them from {MODELS_URL}\n"
            f"or pass --models <dir>."
        )

    started = time.monotonic()
    synth = KokoroSynth(models)
    synth.synthesize("Ready.", "af_heart", 1.0)  # warm-up: first run compiles kernels
    print(
        f"Kokoro ready in {time.monotonic() - started:.1f}s on {synth.provider}, "
        f"{len(synth.voices)} voices",
        flush=True,
    )
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(synth, args.api_key)
    )
    print(f"Listening on http://{args.host}:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
