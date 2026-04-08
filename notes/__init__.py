"""Notes editor module — integrates into the Merlin dashboard."""

import subprocess

import paths

from .routes import router, NOTES_STATIC_DIR
from .sync import start_sync, stop_sync


def _get_current_remote() -> str:
    """Detect current git remote in the notes directory."""
    mem = paths.notes_dir()
    if not (mem / ".git").exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(mem),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


ICON_NOTES = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 6h4"/><path d="M2 10h4"/><path d="M2 14h4"/><rect x="6" y="2" width="16" height="20" rx="2"/><path d="M10 8h8"/><path d="M10 12h8"/><path d="M10 16h8"/></svg>'

NAV_ITEMS = [
    {"url": "/notes", "icon": ICON_NOTES, "label": "Notes"},
]

EXTENSION_META = {
    "name": "Notes",
    "description": "A knowledge base that grows with you — write notes, and let your AI agents curate, connect, and enrich them over time.",
    "icon": ICON_NOTES,
    "config_fields": [
        {
            "key": "NOTES_DIR",
            "label": "Notes Directory",
            "secret": False,
            "required": False,
            "type": "text",
            "placeholder_fn": lambda: f"default: {paths.notes_dir()}",
            "help": "Leave empty to use the default.",
        },
        {
            "key": "NOTES_GIT_SYNC",
            "label": "Git Sync",
            "secret": False,
            "required": False,
            "type": "checkbox",
            "help": "Automatically commit changes and sync with remote.",
        },
        {
            "key": "NOTES_GIT_REMOTE",
            "label": "Git Remote URL",
            "secret": False,
            "required": False,
            "type": "text",
            "depends_on": "NOTES_GIT_SYNC",
            "placeholder_fn": lambda: r if (r := _get_current_remote()) else "",
            "help": "Remote repository URL (e.g. git@github.com:user/notes.git). Leave empty for local-only versioning.",
        },
        {
            "key": "NOTES_SYNC_DEBOUNCE",
            "label": "Sync Debounce (seconds)",
            "secret": False,
            "required": False,
            "type": "text",
            "depends_on": "NOTES_GIT_SYNC",
            "placeholder": "20",
            "help": "Seconds to wait after last change before committing.",
        },
        {
            "key": "NOTES_SYNC_PULL_INTERVAL",
            "label": "Pull Interval (seconds)",
            "secret": False,
            "required": False,
            "type": "text",
            "depends_on": "NOTES_GIT_SYNC",
            "placeholder": "60",
            "help": "Seconds between remote pulls.",
        },
    ],
}


async def start():
    """Start the git sync watcher (if enabled)."""
    await start_sync(paths.notes_dir())


__all__ = [
    "router",
    "NOTES_STATIC_DIR",
    "NAV_ITEMS",
    "EXTENSION_META",
    "start",
    "stop_sync",
]
