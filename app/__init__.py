"""App streaming built-in extension, behind the ``app`` feature flag.

Runs Linux GUI apps on private Xvfb displays, lets an agent drive them through
the ``merlin app`` CLI, and streams them to the dashboard over WebRTC on the
local network. Only loaded when ``MERLIN_FEATURES`` includes ``app`` (see
``features.py``); otherwise it does not exist anywhere.
"""

from pathlib import Path
from typing import Any

ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="14" rx="2"/><path d="M10 9l5 2-5 2z"/><path d="M8 21h8"/></svg>'

URL_SLUG = "apps"

NAV_ITEMS = [{"url": "/apps", "icon": ICON, "label": "Apps"}]

EXTENSION_META = {
    "name": "Apps",
    "description": "Run Linux GUI apps and stream them to your browser over the local network (experimental)",
    "icon": ICON,
}

STATIC_DIR = Path(__file__).parent / "static"


def __getattr__(name: str) -> Any:
    """Keep CLI imports light; load FastAPI routes on server access."""
    if name in {"api_router", "page_router", "register_routes", "start"}:
        from . import routes

        return getattr(routes, name)
    raise AttributeError(name)


__all__ = [
    "EXTENSION_META",
    "NAV_ITEMS",
    "STATIC_DIR",
    "URL_SLUG",
    "api_router",
    "page_router",
    "register_routes",
    "start",
]
