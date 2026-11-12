"""voice/tts.py: which provider the configuration picks, and the ElevenLabs
provider's request and refusals.

Every request goes through an injected ``httpx.MockTransport``: no network.
The conftest clears every ``MERLIN_TTS_*`` key and the Merlin Cloud token
before each test, so each one sets exactly what it means.
"""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voice import routes, session, tts

LUCIE = "YxrwjAKoUKULGd0g8K9Y"
SARAH = "EXAVITQu4vr4xnSDxMaL"


def _env(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(f"MERLIN_TTS_{key}", value)


def _eleven(**overrides):
    values = dict(
        url="https://api.elevenlabs.io/v1",
        api_key="sk_el_SECRET",
        model="eleven_flash_v2_5",
        voice_fr=LUCIE,
        voice_en=SARAH,
        speed=1.0,
        format="mp3",
        provider="elevenlabs",
    )
    values.update(overrides)
    return tts.TtsConfig(**values)


class Recorder:
    """A mock transport that keeps every request and answers with ``reply``."""

    def __init__(self, status=200, content=b"ID3audio", json_body=None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.content = content
        self.json_body = json_body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.json_body is not None:
            return httpx.Response(self.status, json=self.json_body)
        return httpx.Response(self.status, content=self.content)


def _say(text, lang, config, transport):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as c:
            return await tts.synthesize(text, lang, config=config, client=c)

    return asyncio.run(go())


# ---------------------------------------------------------------------------
# Selection (requirements A.2)
# ---------------------------------------------------------------------------


class TestSelection:
    def test_nothing_set_is_not_configured(self):
        assert tts.tts_config() is None
        assert "tts-url" in tts.unconfigured_reason()
        assert "tts-provider elevenlabs" in tts.unconfigured_reason()

    def test_an_existing_url_setup_is_unchanged_with_no_new_key(self, monkeypatch):
        _env(monkeypatch, URL="http://127.0.0.1:8880/v1/", API_KEY="k")
        assert tts.tts_config() == tts.TtsConfig(
            url="http://127.0.0.1:8880/v1",
            api_key="k",
            model="kokoro",
            voice_fr="ff_siwis",
            voice_en="af_heart",
            speed=1.05,
            format="wav",
            provider="openai",
        )

    def test_explicit_openai_is_the_same(self, monkeypatch):
        _env(monkeypatch, URL="http://127.0.0.1:8880/v1", PROVIDER=" OpenAI ")
        config = tts.tts_config()
        assert config is not None and config.provider == "openai"
        assert config.model == "kokoro"

    def test_elevenlabs_with_a_key(self, monkeypatch):
        _env(monkeypatch, PROVIDER="elevenlabs", API_KEY="sk_el")
        assert tts.tts_config() == tts.TtsConfig(
            url="https://api.elevenlabs.io/v1",
            api_key="sk_el",
            model="eleven_flash_v2_5",
            voice_fr=LUCIE,
            voice_en=SARAH,
            speed=1.0,
            format="mp3",
            provider="elevenlabs",
        )

    def test_elevenlabs_takes_the_url_when_set(self, monkeypatch):
        _env(
            monkeypatch,
            PROVIDER="elevenlabs",
            API_KEY="sk_el",
            URL="https://eu.elevenlabs.example/v1/",
        )
        assert tts.tts_config().url == "https://eu.elevenlabs.example/v1"

    def test_elevenlabs_without_a_key_is_a_configuration_error(self, monkeypatch):
        # Even with a URL beside it: the provider says ElevenLabs.
        _env(monkeypatch, PROVIDER="elevenlabs", URL="http://127.0.0.1:8880/v1")
        assert tts.tts_config() is None
        assert tts.unconfigured_reason() == tts.NO_ELEVENLABS_KEY
        assert "tts-api-key" in tts.unconfigured_reason()

    def test_an_unknown_provider_is_a_configuration_error(self, monkeypatch):
        _env(monkeypatch, PROVIDER="kokoro", URL="http://127.0.0.1:8880/v1")
        assert tts.tts_config() is None
        assert tts.unconfigured_reason() == tts.UNKNOWN_PROVIDER

    def test_a_merlin_cloud_token_never_beats_an_explicit_url(self, monkeypatch):
        monkeypatch.setenv("MERLIN_SAAS_TOKEN", "mrl_token")
        _env(monkeypatch, URL="http://127.0.0.1:8880/v1")
        assert tts.tts_config().provider == "openai"

    def test_a_merlin_cloud_token_never_beats_elevenlabs(self, monkeypatch):
        monkeypatch.setenv("MERLIN_SAAS_TOKEN", "mrl_token")
        _env(monkeypatch, PROVIDER="elevenlabs", API_KEY="sk_el")
        assert tts.tts_config().provider == "elevenlabs"


# ---------------------------------------------------------------------------
# Defaults and voices (A.3, A.4)
# ---------------------------------------------------------------------------


class TestDefaultsAndVoices:
    def test_overrides_apply_to_elevenlabs(self, monkeypatch):
        _env(
            monkeypatch,
            PROVIDER="elevenlabs",
            API_KEY="sk_el",
            MODEL="eleven_multilingual_v2",
            VOICE_EN="v_en",
            VOICE_FR="v_fr",
            SPEED="0.9",
        )
        config = tts.tts_config()
        assert (config.model, config.voice_en, config.voice_fr, config.speed) == (
            "eleven_multilingual_v2",
            "v_en",
            "v_fr",
            0.9,
        )

    def test_the_format_key_is_for_openai_only(self, monkeypatch):
        _env(monkeypatch, PROVIDER="elevenlabs", API_KEY="sk_el", FORMAT="wav")
        assert tts.tts_config().format == "mp3"
        monkeypatch.delenv("MERLIN_TTS_PROVIDER")
        _env(monkeypatch, URL="http://127.0.0.1:8880/v1")
        assert tts.tts_config().format == "wav"
        _env(monkeypatch, FORMAT="opus")
        assert tts.tts_config().format == "opus"

    def test_a_bad_speed_falls_back_to_the_providers_default(self, monkeypatch):
        _env(monkeypatch, PROVIDER="elevenlabs", API_KEY="sk_el", SPEED="fast")
        assert tts.tts_config().speed == 1.0
        _env(monkeypatch, PROVIDER="", URL="http://127.0.0.1:8880/v1")
        assert tts.tts_config().speed == 1.05

    def test_a_voice_per_language(self, monkeypatch):
        _env(
            monkeypatch,
            PROVIDER="elevenlabs",
            API_KEY="sk_el",
            VOICE_DE="v_de",
            VOICE_ES="",
        )
        # Not a two-letter code: never read as a language.
        monkeypatch.setenv("MERLIN_TTS_VOICE_DEU", "v_deu")
        monkeypatch.setenv("MERLIN_TTS_VOICE_D1", "v_d1")
        config = tts.tts_config()
        assert config.voices == {"de": "v_de"}
        assert config.voice_for("de") == "v_de"
        assert config.voice_for("DE") == "v_de"
        assert config.voice_for("fr") == LUCIE
        # No voice of its own: the English one.
        assert config.voice_for("es") == SARAH
        assert config.voice_for("deu") == SARAH
        assert config.voice_for("../x") == SARAH
        assert config.voice_for("") == SARAH

    def test_a_voice_per_language_for_openai_too(self, monkeypatch):
        _env(monkeypatch, URL="http://127.0.0.1:8880/v1", VOICE_IT="if_sara")
        config = tts.tts_config()
        assert config.voice_for("it") == "if_sara"
        assert config.voice_for("en") == "af_heart"


# ---------------------------------------------------------------------------
# The ElevenLabs request (A.5)
# ---------------------------------------------------------------------------


class TestElevenLabsRequest:
    def test_url_header_and_body(self):
        rec = Recorder()
        audio = _say("Bonjour.", "fr", _eleven(), rec)
        assert audio == b"ID3audio"
        [request] = rec.requests
        assert request.method == "POST"
        assert str(request.url) == (
            f"https://api.elevenlabs.io/v1/text-to-speech/{LUCIE}"
            "?output_format=mp3_44100_128"
        )
        assert request.headers["xi-api-key"] == "sk_el_SECRET"
        assert "authorization" not in request.headers
        assert json.loads(request.content) == {
            "text": "Bonjour.",
            "model_id": "eleven_flash_v2_5",
            "language_code": "fr",
            "voice_settings": {"speed": 1.0},
        }

    def test_english_takes_sarah(self):
        rec = Recorder()
        _say("Hello.", "en", _eleven(), rec)
        assert rec.requests[0].url.path == f"/v1/text-to-speech/{SARAH}"
        assert json.loads(rec.requests[0].content)["language_code"] == "en"

    @pytest.mark.parametrize(
        "speed, sent", [(0.5, 0.7), (0.7, 0.7), (1.05, 1.05), (1.2, 1.2), (2.0, 1.2)]
    )
    def test_speed_is_clamped_to_the_elevenlabs_range(self, speed, sent):
        rec = Recorder()
        _say("Hi.", "en", _eleven(speed=speed), rec)
        assert json.loads(rec.requests[0].content)["voice_settings"] == {"speed": sent}

    def test_a_voice_id_cannot_change_the_path(self):
        rec = Recorder()
        _say("Hi.", "en", _eleven(voice_en="../../voices?x=1"), rec)
        url = rec.requests[0].url
        assert url.raw_path == (
            b"/v1/text-to-speech/..%2F..%2Fvoices%3Fx%3D1?output_format=mp3_44100_128"
        )
        assert "x" not in url.params

    def test_an_empty_body_is_refused(self):
        with pytest.raises(tts.TtsRefused, match="no audio"):
            _say("Hi.", "en", _eleven(), Recorder(content=b""))

    def test_unreachable_and_timeout(self):
        def down(request):
            raise httpx.ConnectError("refused", request=request)

        def slow(request):
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(tts.TtsUnreachable) as exc:
            _say("Hi.", "en", _eleven(), down)
        assert exc.value.public == "The voice server is unreachable"
        with pytest.raises(tts.TtsUnreachable) as exc:
            _say("Hi.", "en", _eleven(), slow)
        assert exc.value.public == "The voice server timed out"


# ---------------------------------------------------------------------------
# Refusals: fixed public sentences, redacted log detail
# ---------------------------------------------------------------------------


def _detail(status, message):
    # The shape ElevenLabs answers with. Its message may quote the request.
    return {"detail": {"status": status, "message": message}}


class TestElevenLabsRefusals:
    @pytest.mark.parametrize(
        "status, body, public",
        [
            (
                401,
                _detail("invalid_api_key", "Invalid API key: sk_el_SECRET"),
                "The voice service refused the key",
            ),
            (
                403,
                _detail("forbidden", "no access"),
                "The voice service refused the key",
            ),
            (
                402,
                _detail("payment_required", "pay"),
                "The voice service plan does not allow this (quota or paid voice)",
            ),
            (
                401,
                _detail("quota_exceeded", "This request exceeds your quota"),
                "The voice service plan does not allow this (quota or paid voice)",
            ),
            (
                402,
                _detail("paid_plan_required", "Free users cannot use library voices"),
                "The voice service plan does not allow this (quota or paid voice)",
            ),
            (
                400,
                _detail("invalid_text", "the text 'Bonjour.' is bad"),
                "The voice service refused the request (400)",
            ),
            (
                500,
                {"detail": "server down"},
                "The voice service refused the request (500)",
            ),
        ],
    )
    def test_public_sentence_is_fixed(self, status, body, public):
        with pytest.raises(tts.TtsRefused) as exc:
            _say("Bonjour.", "fr", _eleven(), Recorder(status=status, json_body=body))
        assert exc.value.public == public
        # The public sentence never quotes what the service said.
        message = body["detail"]
        said = message["message"] if isinstance(message, dict) else message
        assert said not in exc.value.public
        assert "sk_el_SECRET" not in exc.value.public

    def test_the_log_detail_keeps_the_reason_without_key_or_url(self):
        body = _detail(
            "invalid_api_key",
            "key sk_el_SECRET is wrong for https://api.elevenlabs.io/v1",
        )
        with pytest.raises(tts.TtsRefused) as exc:
            _say("Hi.", "en", _eleven(), Recorder(status=401, json_body=body))
        detail = str(exc.value)
        assert "invalid_api_key" in detail and "401" in detail
        assert "sk_el_SECRET" not in detail
        assert "api.elevenlabs.io" not in detail

    def test_a_plain_text_refusal_is_logged_not_shown(self):
        with pytest.raises(tts.TtsRefused) as exc:
            _say("Hi.", "en", _eleven(), Recorder(status=503, content=b"upstream gone"))
        assert "upstream gone" in str(exc.value)
        assert exc.value.public == "The voice service refused the request (503)"

    def test_openai_sentences_are_unchanged(self):
        config = tts.TtsConfig(
            "http://127.0.0.1:9/v1",
            "s3cret",
            "kokoro",
            "ff_siwis",
            "af_heart",
            1.0,
            "wav",
        )
        body = {"error": {"message": "invalid API key"}}
        with pytest.raises(tts.TtsRefused) as exc:
            _say("Hi.", "en", config, Recorder(status=401, json_body=body))
        assert exc.value.public == "The voice server refused the key (401)"
        assert "invalid API key" in str(exc.value)
        with pytest.raises(tts.TtsRefused) as exc:
            _say("Hi.", "en", config, Recorder(status=402, json_body=body))
        assert exc.value.public == "The voice server refused the request (402)"


# ---------------------------------------------------------------------------
# Content type (A.7) and what the console sees
# ---------------------------------------------------------------------------


class TestContentType:
    def test_elevenlabs_audio_is_served_as_mpeg(self):
        voice = session.VoiceSession("s:@1", "en", config=_eleven(), provider="claude")
        assert voice._content_type() == "audio/mpeg"

    def test_openai_follows_its_format(self, monkeypatch):
        _env(monkeypatch, URL="http://127.0.0.1:8880/v1")
        voice = session.VoiceSession(
            "s:@1", "en", config=tts.tts_config(), provider="claude"
        )
        assert voice._content_type() == "audio/wav"


class TestStatusRoute:
    def _status(self):
        app = FastAPI()
        app.include_router(routes.api_router, prefix="/api/voice")
        return TestClient(app).get("/api/voice/status")

    def test_elevenlabs_shows_the_voice_names_and_no_secret(self, monkeypatch):
        _env(monkeypatch, PROVIDER="elevenlabs", API_KEY="sk_el_SECRET")
        r = self._status()
        assert r.json() == {
            "available": True,
            "reason": None,
            "voices": {"en": "Sarah", "fr": "Lucie"},
        }
        assert "sk_el_SECRET" not in r.text and "elevenlabs.io" not in r.text

    def test_an_unknown_voice_shows_its_id(self, monkeypatch):
        _env(monkeypatch, PROVIDER="elevenlabs", API_KEY="sk_el", VOICE_EN="v_mine")
        assert self._status().json()["voices"] == {"en": "v_mine", "fr": "Lucie"}

    def test_a_missing_key_says_so(self, monkeypatch):
        _env(monkeypatch, PROVIDER="elevenlabs")
        data = self._status().json()
        assert data == {"available": False, "reason": tts.NO_ELEVENLABS_KEY}


# ---------------------------------------------------------------------------
# Validation of the CLI values (A.8)
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize(
        "value, expected",
        [("openai", "openai"), (" ElevenLabs ", "elevenlabs"), ("", "")],
    )
    def test_provider(self, value, expected):
        assert tts.validate_provider(value) == expected

    @pytest.mark.parametrize("value", ["kokoro", "openai\nX=1", "eleven"])
    def test_provider_rejects(self, value):
        with pytest.raises(ValueError):
            tts.validate_provider(value)

    def test_api_key(self):
        assert tts.validate_api_key("  sk_abc  ") == "sk_abc"
        assert tts.validate_api_key("") == ""

    @pytest.mark.parametrize("sep", ["\n", "\r", "\u0085", " ", " ", "\x00"])
    def test_api_key_rejects_without_quoting_it(self, sep):
        with pytest.raises(ValueError) as exc:
            tts.validate_api_key(f"sk_SECRET{sep}X=1")
        assert "SECRET" not in str(exc.value)
