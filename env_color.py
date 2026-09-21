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
    then nothing. Read on every call so a save shows on the next request.
    The file is parsed as the settings API parses it (``_read_config_env``
    in ``main.py``): the last occurrence of a key wins."""
    path = paths.config_path() if config_path is None else config_path
    try:
        text = path.read_text()
    except OSError:
        text = ""
    found = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == KEY:
            found = value.strip()
    if found is not None:
        return found
    return os.environ.get(KEY, "")


def current(config_path=None) -> str:
    """The instance's effective color name, read at request time. The one
    resolver: the settings API, the favicon route, the manifest, the push
    payload and the page all report this name, and derive the hex from it
    with ``accent``."""
    return normalize(_read_setting(config_path))


def palette() -> list[dict[str, str]]:
    """The palette for templates and the API: ``[{name, hex}, ...]``."""
    return [{"name": name, "hex": hex_} for name, hex_ in PALETTE]


_FAVICON_ACCENT_RE = re.compile(re.escape(FAVICON_ACCENT), re.IGNORECASE)


def favicon_svg(source: str, name: str | None) -> str:
    """The favicon SVG with its accent (the mark and the plate's border)
    replaced by the palette color's. The plate fill is not the accent, so it
    stays as it is."""
    return _FAVICON_ACCENT_RE.sub(accent(name), source)


# ---------------------------------------------------------------------------
# The app icons
# ---------------------------------------------------------------------------

# The four committed files per color, with their pixel size. Rendered once at
# build time by ``uv run scripts.py render-icons`` (rsvg-convert) into
# ``static/icons/<color>/``, never on an instance.
ICON_FILES: tuple[tuple[str, int], ...] = (
    ("icon-192.png", 192),
    ("icon-512.png", 512),
    ("icon-maskable-512.png", 512),
    ("apple-touch-icon.png", 180),
)

# The mark's fraction of the plate. iOS masks a home screen icon with a
# squircle and Android's maskable icons with a mask of the same family, both
# biting into the sides, so the mark stays inside the inner 80 percent (a
# circle of diameter 0.8): a square of side 0.56 has a diagonal of 0.79.
APP_ICON_MARK_SCALE = 0.56

_MARK_PATH_RE = re.compile(r'<path fill="[^"]*" d="([^"]+)"')


def mark_path(favicon_source: str) -> str:
    """The hat's path data, read from the favicon so the icons never drift
    from it."""
    m = _MARK_PATH_RE.search(favicon_source)
    if not m:
        raise ValueError("favicon.svg has no mark path")
    return m.group(1)


def app_icon_svg(favicon_source: str, name: str | None) -> str:
    """The SVG an app icon is rendered from: a full-bleed dark plate with no
    border and the favicon's mark centered inside the safe zone, in the
    color's accent. One recipe for the two PWA icons, the maskable icon and
    the Apple touch icon (decision 3 of the environment-color epic)."""
    size = 512
    mark = size * APP_ICON_MARK_SCALE
    offset = (size - mark) / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">\n'
        f'  <rect width="{size}" height="{size}" fill="{PLATE}"/>\n'
        f'  <g transform="translate({offset:g} {offset:g}) scale({APP_ICON_MARK_SCALE})">\n'
        f'    <path fill="{accent(name)}" d="{mark_path(favicon_source)}"/>\n'
        f"  </g>\n"
        f"</svg>\n"
    )


def icon_dir(name: str | None) -> str:
    """The URL prefix of a color's committed app icons."""
    return f"/static/icons/{normalize(name)}"


def icon_url(name: str | None, file: str) -> str:
    """The URL of one of a color's committed app icons."""
    return f"{icon_dir(name)}/{file}"
