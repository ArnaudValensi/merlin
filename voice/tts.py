"""Text-to-speech client: one OpenAI-compatible ``audio/speech`` endpoint.

Merlin speaks only the OpenAI API (``POST {base}/audio/speech`` with
``model``, ``input``, ``voice``, ``response_format``, ``speed``; audio bytes
back). Any server or cloud provider that speaks it plugs in through
``MERLIN_TTS_URL``. The reference server is ``tools/kokoro-tts-server.py``;
nothing here knows which server answers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

DEFAULT_MODEL = "kokoro"
DEFAULT_VOICE_FR = "ff_siwis"
DEFAULT_VOICE_EN = "af_heart"
DEFAULT_SPEED = 1.05
DEFAULT_FORMAT = "wav"

# Long enough for a few sentences on a cold GPU, short enough that a dead
# server turns into an error the console can say rather than a stuck answer.
TIMEOUT = httpx.Timeout(30.0, connect=3.0)

URL_KEY = "MERLIN_TTS_URL"


@dataclass(frozen=True)
class TtsConfig:
    url: str
    api_key: str
    model: str
    voice_fr: str
    voice_en: str
    speed: float
    format: str

    def voice_for(self, lang: str) -> str:
        return self.voice_fr if lang == "fr" else self.voice_en


class TtsError(Exception):
    """A synthesis that failed, with a message fit for the voice console."""

    kind = "error"


class TtsNotConfigured(TtsError):
    kind = "not_configured"


class TtsUnreachable(TtsError):
    kind = "unreachable"


class TtsRefused(TtsError):
    kind = "refused"


def _speed(raw: str | None) -> float:
    try:
        value = float(raw) if raw else DEFAULT_SPEED
    except ValueError:
        return DEFAULT_SPEED
    return value if 0.25 <= value <= 4.0 else DEFAULT_SPEED


def tts_config() -> TtsConfig | None:
    """The configured endpoint, or None when ``MERLIN_TTS_URL`` is unset."""
    url = os.environ.get(URL_KEY, "").strip().rstrip("/")
    if not url:
        return None
    env = os.environ.get
    return TtsConfig(
        url=url,
        api_key=env("MERLIN_TTS_API_KEY", "").strip(),
        model=env("MERLIN_TTS_MODEL", "").strip() or DEFAULT_MODEL,
        voice_fr=env("MERLIN_TTS_VOICE_FR", "").strip() or DEFAULT_VOICE_FR,
        voice_en=env("MERLIN_TTS_VOICE_EN", "").strip() or DEFAULT_VOICE_EN,
        speed=_speed(env("MERLIN_TTS_SPEED")),
        format=env("MERLIN_TTS_FORMAT", "").strip() or DEFAULT_FORMAT,
    )


def validate_url(value: str) -> str:
    """Normalize a base URL for ``merlin config tts-url``. Empty turns it off.

    Raises ValueError for anything that is not an http(s) URL with a host.
    """
    value = value.strip().rstrip("/")
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(
            f"invalid tts-url {value!r}; expected the API root, "
            "for example http://127.0.0.1:8880/v1"
        )
    return value


def _refusal_detail(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
        message = error.get("message") if isinstance(error, dict) else error
    except (ValueError, AttributeError):
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
) -> bytes:
    """Speak ``text`` in the voice for ``lang``. Returns the audio bytes.

    Raises TtsNotConfigured, TtsUnreachable (no answer, timeout) or
    TtsRefused (an HTTP error or an empty body).
    """
    config = config or tts_config()
    if config is None:
        raise TtsNotConfigured("voice server not configured")
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    body = {
        "model": config.model,
        "input": text,
        "voice": config.voice_for(lang),
        "response_format": config.format,
        "speed": config.speed,
    }
    url = f"{config.url}/audio/speech"
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT) as own:
                response = await own.post(url, json=body, headers=headers)
        else:
            response = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException as e:
        raise TtsUnreachable("voice server timed out") from e
    except httpx.HTTPError as e:
        raise TtsUnreachable("voice server unreachable") from e
    if response.status_code != 200:
        raise TtsRefused(
            f"voice server refused the request ({response.status_code}: "
            f"{_refusal_detail(response)})"
        )
    if not response.content:
        raise TtsRefused("voice server returned no audio")
    return response.content
