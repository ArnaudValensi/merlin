"""voice/tts.py against a local OpenAI-compatible server, and that server.

The server is the HTTP layer of ``tools/kokoro-tts-server.py`` with a fake
synthesizer in place of Kokoro, so the client and the reference server are
checked against each other without a model or a GPU.
"""

import asyncio
import importlib.util
import io
import json
import socket
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voice import routes, tts

SERVER_PATH = Path(__file__).parents[2] / "tools" / "kokoro-tts-server.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("kokoro_tts_server", SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server_mod = _load_server()


class FakeSynth:
    """A tone-free stand-in: 0.1 s of a constant non-zero sample per call."""

    voices = ["af_heart", "ff_siwis", "bm_george"]
    provider = "FakeProvider"

    def __init__(self):
        self.calls = []

    def synthesize(self, text, voice, speed):
        self.calls.append((text, voice, speed))
        return (1000).to_bytes(2, "little", signed=True) * 2400


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    return server


@pytest.fixture
def synth():
    return FakeSynth()


@pytest.fixture
def server(synth):
    srv = _serve(server_mod.make_handler(synth, api_key="s3cret"))
    yield srv
    srv.shutdown()
    srv.server_close()


def _config(srv, **overrides):
    host, port = srv.server_address[:2]
    values = dict(
        url=f"http://{host}:{port}/v1",
        api_key="s3cret",
        model="kokoro",
        voice_fr="ff_siwis",
        voice_en="af_heart",
        speed=1.05,
        format="wav",
    )
    values.update(overrides)
    return tts.TtsConfig(**values)


def _say(text, lang, config, **kw):
    return asyncio.run(tts.synthesize(text, lang, config=config, **kw))


class TestConfig:
    def test_unset_url_means_not_configured(self, monkeypatch):
        monkeypatch.delenv("MERLIN_TTS_URL", raising=False)
        assert tts.tts_config() is None

    def test_defaults(self, monkeypatch):
        for key in ("API_KEY", "MODEL", "VOICE_FR", "VOICE_EN", "SPEED", "FORMAT"):
            monkeypatch.delenv(f"MERLIN_TTS_{key}", raising=False)
        monkeypatch.setenv("MERLIN_TTS_URL", "http://127.0.0.1:8880/v1/")
        config = tts.tts_config()
        assert config == tts.TtsConfig(
            url="http://127.0.0.1:8880/v1",
            api_key="",
            model="kokoro",
            voice_fr="ff_siwis",
            voice_en="af_heart",
            speed=1.05,
            format="wav",
        )
        assert config.voice_for("fr") == "ff_siwis"
        assert config.voice_for("en") == "af_heart"

    def test_overrides_and_bad_speed(self, monkeypatch):
        monkeypatch.setenv("MERLIN_TTS_URL", "https://tts.example/v1")
        monkeypatch.setenv("MERLIN_TTS_VOICE_EN", "bm_george")
        monkeypatch.setenv("MERLIN_TTS_SPEED", "fast")
        config = tts.tts_config()
        assert config.voice_en == "bm_george"
        assert config.speed == tts.DEFAULT_SPEED

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("http://127.0.0.1:8880/v1/", "http://127.0.0.1:8880/v1"),
            ("  https://api.example.com/v1 ", "https://api.example.com/v1"),
            ("", ""),
        ],
    )
    def test_validate_url(self, value, expected):
        assert tts.validate_url(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "localhost:8880",
            "ftp://x/v1",
            "http://",
            "http://localhost:8880/v1\nUNRELATED_KEY=changed",
            "http://localhost:8880/v1\rX=1",
            "http://local\x00host/v1",
            "http://localhost/v1\x7f",
            "http://localhost/v1\u0085X=1",
            "http://localhost/v1\u2028X=1",
            "http://localhost/v1\u2029X=1",
            "http://localhost/my v1",
        ],
    )
    def test_validate_url_rejects(self, value):
        with pytest.raises(ValueError):
            tts.validate_url(value)


class TestSynthesize:
    def test_request_shape_and_wav_back(self, server, synth):
        audio = _say("Bonjour.", "fr", _config(server))
        assert synth.calls == [("Bonjour.", "ff_siwis", 1.05)]
        with wave.open(io.BytesIO(audio)) as w:
            assert w.getframerate() == 24000
            assert w.getnchannels() == 1
            assert w.getnframes() == 2400

    def test_body_and_auth_header_as_sent(self):
        seen = {}

        class Capture(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen["path"] = self.path
                seen["auth"] = self.headers.get("Authorization")
                seen["body"] = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                self.send_response(200)
                self.send_header("Content-Length", "3")
                self.end_headers()
                self.wfile.write(b"abc")

        srv = _serve(Capture)
        try:
            audio = _say("Hello.", "en", _config(srv, api_key="k1", format="pcm"))
        finally:
            srv.shutdown()
            srv.server_close()
        assert audio == b"abc"
        assert seen["path"] == "/v1/audio/speech"
        assert seen["auth"] == "Bearer k1"
        assert seen["body"] == {
            "model": "kokoro",
            "input": "Hello.",
            "voice": "af_heart",
            "response_format": "pcm",
            "speed": 1.05,
        }

    def test_no_key_sends_no_auth_header(self):
        seen = {}

        class Capture(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen["auth"] = self.headers.get("Authorization")
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(b"x")

        srv = _serve(Capture)
        try:
            _say("Hi.", "en", _config(srv, api_key=""))
        finally:
            srv.shutdown()
            srv.server_close()
        assert seen["auth"] is None

    def test_wrong_key_is_refused_with_the_reason(self, server):
        with pytest.raises(tts.TtsRefused, match="401: invalid API key"):
            _say("Hi.", "en", _config(server, api_key="nope"))

    def test_unknown_voice_is_refused(self, server):
        with pytest.raises(tts.TtsRefused, match="unknown voice"):
            _say("Hi.", "en", _config(server, voice_en="zz_nobody"))

    def test_unreachable(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        config = tts.TtsConfig(
            f"http://127.0.0.1:{port}/v1",
            "",
            "kokoro",
            "ff_siwis",
            "af_heart",
            1.0,
            "wav",
        )
        with pytest.raises(tts.TtsUnreachable, match="unreachable"):
            _say("Hi.", "en", config)

    def test_timeout(self):
        class Slow(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                time.sleep(1.0)

        srv = _serve(Slow)
        try:

            async def go():
                async with httpx.AsyncClient(timeout=0.2) as client:
                    await tts.synthesize(
                        "Hi.", "en", config=_config(srv), client=client
                    )

            with pytest.raises(tts.TtsUnreachable, match="timed out"):
                asyncio.run(go())
        finally:
            srv.shutdown()
            srv.server_close()

    def _trickle_server(self):
        class Trickle(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "40")
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.04)
                except OSError:
                    pass

        return _serve(Trickle)

    def test_a_trickling_body_hits_the_overall_deadline(self):
        srv = self._trickle_server()
        try:
            started = time.monotonic()
            with pytest.raises(tts.TtsUnreachable, match="timed out"):
                _say("Hi.", "en", _config(srv), deadline=0.3)
            assert time.monotonic() - started < 1.0
        finally:
            srv.shutdown()
            srv.server_close()

    def test_deadline_holds_for_an_injected_client_without_timeout(self):
        srv = self._trickle_server()
        try:

            async def go():
                async with httpx.AsyncClient(timeout=None) as client:
                    await tts.synthesize(
                        "Hi.", "en", config=_config(srv), client=client, deadline=0.3
                    )

            with pytest.raises(tts.TtsUnreachable, match="timed out"):
                asyncio.run(go())
        finally:
            srv.shutdown()
            srv.server_close()

    def test_external_cancellation_is_not_a_tts_error(self):
        srv = self._trickle_server()
        try:

            async def go():
                task = asyncio.create_task(
                    tts.synthesize("Hi.", "en", config=_config(srv), deadline=5)
                )
                await asyncio.sleep(0.2)
                task.cancel()
                await task

            with pytest.raises(asyncio.CancelledError):
                asyncio.run(go())
        finally:
            srv.shutdown()
            srv.server_close()

    def test_empty_body_is_refused(self):
        class Empty(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        srv = _serve(Empty)
        try:
            with pytest.raises(tts.TtsRefused, match="no audio"):
                _say("Hi.", "en", _config(srv))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("MERLIN_TTS_URL", raising=False)
        with pytest.raises(tts.TtsNotConfigured):
            asyncio.run(tts.synthesize("Hi.", "en"))


class TestReferenceServer:
    def _post(self, srv, body, key="s3cret"):
        host, port = srv.server_address[:2]
        return httpx.post(
            f"http://{host}:{port}/v1/audio/speech",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )

    def test_pcm_is_raw_samples(self, server):
        r = self._post(
            server, {"input": "Hi.", "voice": "af_heart", "response_format": "pcm"}
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/pcm"
        assert len(r.content) == 4800

    @pytest.mark.parametrize(
        "body, message",
        [
            ({"voice": "af_heart"}, "'input' is required"),
            ({"input": "  "}, "'input' is required"),
            ({"input": "x" * 5000}, "longer than"),
            ({"input": "Hi.", "response_format": "mp3"}, "not supported"),
            ({"input": "Hi.", "speed": 9}, "between"),
            ({"input": "Hi.", "speed": "quick"}, "must be a number"),
        ],
    )
    def test_bad_requests_get_a_clear_400(self, server, body, message):
        r = self._post(server, body)
        assert r.status_code == 400
        assert message in r.json()["error"]["message"]

    def test_model_is_accepted_and_ignored(self, server, synth):
        r = self._post(server, {"input": "Hi.", "model": "tts-1", "voice": "bm_george"})
        assert r.status_code == 200
        assert synth.calls[-1][1] == "bm_george"

    def test_discovery_routes(self, server):
        host, port = server.server_address[:2]
        base = f"http://{host}:{port}"
        assert httpx.get(f"{base}/health").json() == {
            "status": "ok",
            "provider": "FakeProvider",
            "voices": 3,
        }
        auth = {"Authorization": "Bearer s3cret"}
        assert httpx.get(f"{base}/v1/models", headers=auth).json()["data"][0]["id"] == (
            "kokoro"
        )
        assert httpx.get(f"{base}/v1/models").status_code == 401
        assert httpx.get(f"{base}/v1/audio/voices", headers=auth).json() == {
            "voices": FakeSynth.voices
        }

    def test_voice_language_from_the_prefix(self):
        assert server_mod.voice_lang("ff_siwis") == "fr-fr"
        assert server_mod.voice_lang("bm_george") == "en-gb"
        assert server_mod.voice_lang("af_heart") == "en-us"

    def test_help_runs_without_the_model(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["kokoro-tts-server.py", "--help"])
        with pytest.raises(SystemExit) as exc:
            server_mod.main()
        assert exc.value.code == 0
        assert "/v1/audio/speech" in capsys.readouterr().out


class TestStatusRoute:
    def _client(self):
        app = FastAPI()
        app.include_router(routes.api_router, prefix="/api/voice")
        return TestClient(app)

    def test_unavailable_says_why(self, monkeypatch):
        monkeypatch.delenv("MERLIN_TTS_URL", raising=False)
        data = self._client().get("/api/voice/status").json()
        assert data["available"] is False
        assert "tts-url" in data["reason"]

    def test_available_never_leaks_the_url_or_key(self, monkeypatch):
        monkeypatch.setenv("MERLIN_TTS_URL", "http://10.0.0.5:8880/v1")
        monkeypatch.setenv("MERLIN_TTS_API_KEY", "top-secret")
        r = self._client().get("/api/voice/status")
        assert r.json()["available"] is True
        assert "10.0.0.5" not in r.text and "top-secret" not in r.text

    def test_mounted_in_the_app(self):
        import main

        assert "/api/voice/status" in {getattr(r, "path", "") for r in main.app.routes}
