"""Workspace restore: continuous tmux snapshot, restore offer after a restart.

See ``docs/dev/workspace-restore.md``.
"""

from .routes import STATIC_DIR, URL_SLUG, api_router

__all__ = ["STATIC_DIR", "URL_SLUG", "api_router"]
