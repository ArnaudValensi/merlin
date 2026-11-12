"""Text-to-speech client: one ``synthesize()`` over a few providers.

- **openai**: any OpenAI-compatible ``POST {base}/audio/speech`` server
  (``model``, ``input``, ``voice``, ``response_format``, ``speed``; audio bytes
  back), set by ``MERLIN_TTS_URL``. The reference server is
  ``tools/kokoro-tts-server.py``; nothing here knows which server answers.
- **elevenlabs**: ElevenLabs' own ``text-to-speech`` endpoint with the user's
  key (``MERLIN_TTS_PROVIDER=elevenlabs``, ``MERLIN_TTS_API_KEY``).

Explicit configuration wins: see ``tts_config()`` for the order. Every
provider answers through the same typed errors, whose ``public`` sentence is
fixed and never quotes the service, the URL or the key.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

import httpx

OPENAI = "openai"
ELEVENLABS = "elevenlabs"
PROVIDERS = (OPENAI, ELEVENLABS)

# The OpenAI-compatible defaults are the reference Kokoro server's.
DEFAULT_MODEL = "kokoro"
DEFAULT_VOICE_FR = "ff_siwis"
DEFAULT_VOICE_EN = "af_heart"
DEFAULT_SPEED = 1.05
DEFAULT_FORMAT = "wav"

ELEVENLABS_URL = "https://api.elevenlabs.io/v1"
ELEVENLABS_MODEL = "eleven_flash_v2_5"
# Chosen by ear (epics/cli/voice-cloud-tts/research.md). Lucie is a library
# voice (a paid plan), Sarah one of ElevenLabs' default voices.
ELEVENLABS_VOICE_FR = "YxrwjAKoUKULGd0g8K9Y"
ELEVENLABS_VOICE_EN = "EXAVITQu4vr4xnSDxMaL"
ELEVENLABS_SPEED = 1.0
ELEVENLABS_OUTPUT = "mp3_44100_128"
# ElevenLabs refuses a speed outside this range.
ELEVENLABS_SPEED_RANGE = (0.7, 1.2)

# What the console may show for a voice id it knows.
VOICE_NAMES = {ELEVENLABS_VOICE_FR: "Lucie", ELEVENLABS_VOICE_EN: "Sarah"}

# Long enough for a few sentences on a cold GPU, short enough that a dead
# server turns into an error the console can say rather than a stuck answer.
# DEADLINE bounds the whole request, body included: httpx timeouts are per
# network operation, so a server trickling bytes would never trip them.
DEADLINE = 30.0
TIMEOUT = httpx.Timeout(DEADLINE, connect=3.0)

URL_KEY = "MERLIN_TTS_URL"
PROVIDER_KEY = "MERLIN_TTS_PROVIDER"
API_KEY_KEY = "MERLIN_TTS_API_KEY"
_VOICE_KEY = re.compile(r"MERLIN_TTS_VOICE_([A-Z]{2})")
_LANG = re.compile(r"[a-z]{2}")

NOT_CONFIGURED = (
    "No voice server configured (merlin config tts-url <url>, "
    "or merlin config tts-provider elevenlabs with a key)"
)
NO_ELEVENLABS_KEY = "The ElevenLabs voice needs a key (merlin config tts-api-key <key>)"
UNKNOWN_PROVIDER = (
    "Unknown voice provider (merlin config tts-provider openai or elevenlabs)"
)


@dataclass(frozen=True)
class TtsConfig:
    url: str
    api_key: str
    model: str
    voice_fr: str
    voice_en: str
    speed: float
    # The effective audio format: what the bytes are, so what the page is
    # told they are (``mp3`` for ElevenLabs).
    format: str
    provider: str = OPENAI
    # Voices for languages other than English and French, by ISO 639-1 code.
    voices: Mapping[str, str] = field(default_factory=dict)

    def voice_for(self, lang: str) -> str:
        """The voice for ``lang``, the English one for a language without."""
        lang = (lang or "").strip().lower()
        if lang == "fr":
            return self.voice_fr
        if lang != "en" and _LANG.fullmatch(lang):
            return self.voices.get(lang) or self.voice_en
        return self.voice_en


class TtsError(Exception):
    """A synthesis that failed.

    ``str(e)`` carries the detail for the server log, which may quote the
    voice server's own answer. ``public`` is the browser-safe message: it
    never holds what the server said, nor the URL or the key.
    """

    kind = "error"

    def __init__(self, detail: str, public: str = "The voice failed") -> None:
        super().__init__(detail)
        self.public = public


class TtsNotConfigured(TtsError):
    kind = "not_configured"


class TtsUnreachable(TtsError):
    kind = "unreachable"


class TtsRefused(TtsError):
    kind = "refused"


def redact(text: str, config: TtsConfig | None) -> str:
    """``text`` without the configured URL or key, for the server log."""
    if config is not None:
        for secret in (config.api_key, config.url):
            if secret:
                text = text.replace(secret, "[redacted]")
    return text


def _speed(raw: str | None, default: float) -> float:
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if 0.25 <= value <= 4.0 else default


def _other_voices() -> dict[str, str]:
    """``MERLIN_TTS_VOICE_<LANG>`` for every language but English and French
    (those two are fields of their own)."""
    voices = {}
    for key, value in os.environ.items():
        match = _VOICE_KEY.fullmatch(key)
        if match and value.strip():
            lang = match.group(1).lower()
            if lang not in ("en", "fr"):
                voices[lang] = value.strip()
    return voices


def _provider_setting() -> str:
    return os.environ.get(PROVIDER_KEY, "").strip().lower()


def tts_config() -> TtsConfig | None:
    """The configured provider, or None when there is none.

    In this order, explicit configuration first:

    1. ``MERLIN_TTS_PROVIDER=elevenlabs`` with a key: ElevenLabs
       (``MERLIN_TTS_URL`` optional, ElevenLabs' API by default);
    2. ``MERLIN_TTS_URL`` set, provider ``openai`` or unset: OpenAI-compatible;
    3. otherwise none. ``elevenlabs`` without a key, or a provider Merlin does
       not know, is a configuration error and none too: ``unconfigured_reason()``
       says which.
    """
    env = os.environ.get
    provider = _provider_setting()
    url = env(URL_KEY, "").strip().rstrip("/")
    api_key = env(API_KEY_KEY, "").strip()
    model = env("MERLIN_TTS_MODEL", "").strip()
    voice_fr = env("MERLIN_TTS_VOICE_FR", "").strip()
    voice_en = env("MERLIN_TTS_VOICE_EN", "").strip()
    speed = env("MERLIN_TTS_SPEED")
    if provider == ELEVENLABS:
        if not api_key:
            return None
        return TtsConfig(
            url=url or ELEVENLABS_URL,
            api_key=api_key,
            model=model or ELEVENLABS_MODEL,
            voice_fr=voice_fr or ELEVENLABS_VOICE_FR,
            voice_en=voice_en or ELEVENLABS_VOICE_EN,
            speed=_speed(speed, ELEVENLABS_SPEED),
            format="mp3",
            provider=ELEVENLABS,
            voices=_other_voices(),
        )
    if provider not in ("", OPENAI) or not url:
        return None
    return TtsConfig(
        url=url,
        api_key=api_key,
        model=model or DEFAULT_MODEL,
        voice_fr=voice_fr or DEFAULT_VOICE_FR,
        voice_en=voice_en or DEFAULT_VOICE_EN,
        speed=_speed(speed, DEFAULT_SPEED),
        format=env("MERLIN_TTS_FORMAT", "").strip() or DEFAULT_FORMAT,
        provider=OPENAI,
        voices=_other_voices(),
    )


def unconfigured_reason() -> str:
    """Why ``tts_config()`` found nothing, as a sentence for the console."""
    provider = _provider_setting()
    if provider == ELEVENLABS:
        return NO_ELEVENLABS_KEY
    if provider not in ("", OPENAI):
        return UNKNOWN_PROVIDER
    return NOT_CONFIGURED


def voice_names(config: TtsConfig) -> dict[str, str]:
    """The console's languages and the voice each speaks with: a known
    voice by its name, any other by its id."""
    return {
        lang: VOICE_NAMES.get(voice, voice)
        for lang, voice in (("en", config.voice_en), ("fr", config.voice_fr))
    }


def validate_provider(value: str) -> str:
    """Normalize a provider for ``merlin config tts-provider``. Empty clears it.

    Raises ValueError for a provider Merlin does not know.
    """
    value = value.strip().lower()
    if value and value not in PROVIDERS:
        raise ValueError(
            f"invalid tts-provider {value!r}; expected {' or '.join(PROVIDERS)}, "
            "or an empty value to clear it"
        )
    return value


def _single_line(value: str, key: str) -> None:
    # config.env is one KEY=VALUE per line, split with str.splitlines(), which
    # also breaks on U+0085, U+2028 and U+2029: any such character would write
    # a second key. A URL carries no raw whitespace either (spaces are
    # percent-encoded), and neither does an API key.
    if any(c.isspace() or not c.isprintable() for c in value):
        raise ValueError(f"invalid {key}: it contains whitespace or control characters")


def validate_api_key(value: str) -> str:
    """Normalize a key for ``merlin config tts-api-key``. Empty clears it.

    Raises ValueError for a key that would not fit on one config.env line. The
    message never quotes the key.
    """
    value = value.strip()
    _single_line(value, "tts-api-key")
    return value


def validate_url(value: str) -> str:
    """Normalize a base URL for ``merlin config tts-url``. Empty turns it off.

    Raises ValueError for anything that is not an http(s) URL with a host.
    """
    value = value.strip().rstrip("/")
    if not value:
        return ""
    # urlsplit drops embedded newlines while parsing: check the raw value.
    _single_line(value, "tts-url")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(
            f"invalid tts-url {value!r}; expected the API root, "
            "for example http://127.0.0.1:8880/v1"
        )
    return value


# ---------------------------------------------------------------------------
# Providers: each turns a text into a request, and a refusal into a sentence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Request:
    url: str
    headers: dict[str, str]
    body: dict
    params: dict[str, str] = field(default_factory=dict)


def _openai_request(text: str, lang: str, config: TtsConfig) -> _Request:
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    return _Request(
        url=f"{config.url}/audio/speech",
        headers=headers,
        body={
            "model": config.model,
            "input": text,
            "voice": config.voice_for(lang),
            "response_format": config.format,
            "speed": config.speed,
        },
    )


def _openai_public(status: int, detail: str) -> str:
    if status in (401, 403):
        return f"The voice server refused the key ({status})"
    return f"The voice server refused the request ({status})"


def clamp_speed(speed: float) -> float:
    low, high = ELEVENLABS_SPEED_RANGE
    return min(max(speed, low), high)


def _elevenlabs_request(text: str, lang: str, config: TtsConfig) -> _Request:
    voice = quote(config.voice_for(lang), safe="")
    code = (lang or "").strip().lower()
    return _Request(
        url=f"{config.url}/text-to-speech/{voice}",
        headers={"xi-api-key": config.api_key},
        params={"output_format": ELEVENLABS_OUTPUT},
        body={
            "text": text,
            "model_id": config.model,
            "language_code": code if _LANG.fullmatch(code) else "en",
            "voice_settings": {"speed": clamp_speed(config.speed)},
        },
    )


# ElevenLabs answers a used-up quota or a library voice on a free plan with
# these in ``detail.status``, not always with a 402.
_PLAN_REFUSALS = ("quota_exceeded", "paid_plan_required")


def _elevenlabs_public(status: int, detail: str) -> str:
    if status == 402 or any(word in detail for word in _PLAN_REFUSALS):
        return "The voice service plan does not allow this (quota or paid voice)"
    if status in (401, 403):
        return "The voice service refused the key"
    return f"The voice service refused the request ({status})"


_PROVIDERS = {
    OPENAI: (_openai_request, _openai_public),
    ELEVENLABS: (_elevenlabs_request, _elevenlabs_public),
}


def _refusal_detail(response: httpx.Response) -> str:
    """What the service said, for the log only. OpenAI-style
    ``{"error": {"message"}}``, ElevenLabs-style ``{"detail": {"status",
    "message"}}``, or the start of the body."""
    message = None
    try:
        data = response.json()
        if isinstance(data, dict):
            error = data.get("error") or data.get("detail")
            if isinstance(error, dict):
                parts = [error.get("status"), error.get("message")]
                message = ": ".join(str(p) for p in parts if p) or None
            elif error:
                message = str(error)
    except ValueError:
        message = None
    if not message:
        message = response.text[:200].strip()
    return str(message or response.reason_phrase)


async def synthesize(
    text: str,
    lang: str,
    *,
    config: TtsConfig | None = None,
    client: httpx.AsyncClient | None = None,
    deadline: float = DEADLINE,
) -> bytes:
    """Speak ``text`` in the voice for ``lang``. Returns the audio bytes.

    The whole request, body included, must finish within ``deadline``
    seconds, whatever timeout an injected ``client`` carries.

    Raises TtsNotConfigured, TtsUnreachable (no answer, timeout) or
    TtsRefused (an HTTP error or an empty body).
    """
    config = config or tts_config()
    if config is None:
        raise TtsNotConfigured(
            "voice server not configured", "No voice server configured"
        )
    build, public = _PROVIDERS.get(config.provider, _PROVIDERS[OPENAI])
    request = build(text, lang, config)
    params = request.params or None
    try:
        async with asyncio.timeout(deadline):
            if client is None:
                async with httpx.AsyncClient(timeout=TIMEOUT) as own:
                    response = await own.post(
                        request.url,
                        json=request.body,
                        headers=request.headers,
                        params=params,
                    )
            else:
                response = await client.post(
                    request.url,
                    json=request.body,
                    headers=request.headers,
                    params=params,
                )
    except (httpx.TimeoutException, TimeoutError) as e:
        raise TtsUnreachable(
            "voice server timed out", "The voice server timed out"
        ) from e
    except httpx.HTTPError as e:
        raise TtsUnreachable(
            "voice server unreachable", "The voice server is unreachable"
        ) from e
    status = response.status_code
    if status != 200:
        said = _refusal_detail(response)
        detail = redact(f"voice server refused the request ({status}: {said})", config)
        raise TtsRefused(detail, public(status, said))
    if not response.content:
        raise TtsRefused(
            "voice server returned no audio", "The voice server returned no audio"
        )
    return response.content
