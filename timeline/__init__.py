"""Agent activity Timeline built-in extension."""

import asyncio
from pathlib import Path
from typing import Any


ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M3 12h18"/><path d="M3 18h18"/><path d="M7 4v4"/><path d="M15 10v4"/><path d="M11 16v4"/></svg>'

NAV_ITEMS = [{"url": "/timeline", "icon": ICON, "label": "Timeline"}]

EXTENSION_META = {
    "name": "Timeline",
    "description": "Private activity history for interactive agents and automation",
    "icon": ICON,
}

STATIC_DIR = Path(__file__).parent / "static"


async def start() -> None:
    """Reconcile this extension's provider hooks after server startup."""
    from .reconcile import sync_hooks

    await asyncio.to_thread(sync_hooks)


def disable() -> None:
    """Stop capture before the extension's web surfaces are disabled."""
    from .consent import set_capture_mode
    from .reconcile import remove_hooks

    set_capture_mode("off")
    remove_hooks()


def __getattr__(name: str) -> Any:
    """Keep command and hook imports light; load FastAPI routes on server access."""
    if name in {"api_router", "page_router"}:
        from . import routes

        return getattr(routes, name)
    raise AttributeError(name)


__all__ = [
    "EXTENSION_META",
    "NAV_ITEMS",
    "STATIC_DIR",
    "api_router",
    "disable",
    "page_router",
    "start",
]
