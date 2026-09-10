"""Notifications: attention events from tmux, in-tab notifications, the
installable app and Web Push. Three files with fixed responsibilities (kept as
seams for the hub epic): ``watcher.py`` produces events and knows nothing about
delivery, ``push.py`` sends and knows nothing about tmux, ``routes.py`` glues
them and serves the API and the popover's assets.

The singleton watcher is ``notifications.watcher.watcher`` (the submodule keeps
its name, so the instance is not re-exported here).
"""

from .routes import STATIC_DIR, api_router
from .watcher import Event, Watcher

__all__ = ["STATIC_DIR", "Event", "Watcher", "api_router"]
