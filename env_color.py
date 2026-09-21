"""The environment color: one palette color per instance, so a person running
several Merlin environments tells them apart by the favicon, the app icon, the
push icon and the sidebar's brand mark before reading a word.

A fixed palette of named colors, never a free value: the app icons are PNGs
rendered once at build time and committed per color (``scripts.py
render-icons``), so an instance needs no rasterizer. The setting is
``MERLIN_ENV_COLOR`` in ``config.env``, written through the settings API and
read at request time, so a change shows on the next page load and the next
push with no restart. An absent or unknown value is the default green, never
an error.

Only the accent moves. The favicon's plate stays dark, the dashboard keeps the
design system's green on every control, the manifest's theme and background
colors stay the page background.
"""

from __future__ import annotations

import os
import re

import paths

KEY = "MERLIN_ENV_COLOR"

# The current green first, as the default, then a spread that stays apart on
# a tab strip and on the dark plate.
PALETTE: tuple[tuple[str, str], ...] = (
    ("green", "#4ade80"),
    ("blue", "#60a5fa"),
    ("violet", "#a78bfa"),
    ("pink", "#f472b6"),
    ("red", "#f87171"),
    ("orange", "#fb923c"),
    ("yellow", "#facc15"),
    ("cyan", "#22d3ee"),
)
DEFAULT = PALETTE[0][0]
NAMES: tuple[str, ...] = tuple(name for name, _ in PALETTE)
_ACCENT: dict[str, str] = dict(PALETTE)

# The favicon's own accent, the one the served route substitutes. The plate
# fill (#1e2035) is left alone.
FAVICON_ACCENT = _ACCENT[DEFAULT]
PLATE = "#1e2035"


def is_valid(value: str | None) -> bool:
    """Whether ``value`` names a palette color, whatever its case or spacing."""
    return (value or "").strip().lower() in _ACCENT


def normalize(value: str | None) -> str:
    """The palette name for a setting value: lowercased and trimmed, and the
    default for an absent or unknown value. Never raises."""
    name = (value or "").strip().lower()
    return name if name in _ACCENT else DEFAULT


def accent(name: str | None) -> str:
    """The accent hex of a palette name (or of the default for an unknown one)."""
    return _ACCENT[normalize(name)]


def _read_setting(config_path=None) -> str:
    """The raw setting: ``config.env`` first (what the settings API writes),
    then the process environment (a value set by whoever started Merlin),
    then nothing. Read on every call so a save shows on the next request."""
    path = paths.config_path() if config_path is None else config_path
    try:
        text = path.read_text()
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == KEY:
            return value.strip()
    return os.environ.get(KEY, "")


def current(config_path=None) -> str:
    """The instance's color name, read at request time."""
    return normalize(_read_setting(config_path))


def current_accent(config_path=None) -> str:
    """The instance's accent hex, read at request time."""
    return accent(current(config_path))


def palette() -> list[dict[str, str]]:
    """The palette for templates and the API: ``[{name, hex}, ...]``."""
    return [{"name": name, "hex": hex_} for name, hex_ in PALETTE]


_FAVICON_ACCENT_RE = re.compile(re.escape(FAVICON_ACCENT), re.IGNORECASE)


def favicon_svg(source: str, name: str | None) -> str:
    """The favicon SVG with its accent (the mark and the plate's border)
    replaced by the palette color's. The plate fill is not the accent, so it
    stays as it is."""
    return _FAVICON_ACCENT_RE.sub(accent(name), source)


def icon_dir(name: str | None) -> str:
    """The URL prefix of a color's committed app icons."""
    return f"/static/icons/{normalize(name)}"


def icon_url(name: str | None, file: str) -> str:
    """The URL of one of a color's committed app icons."""
    return f"{icon_dir(name)}/{file}"
