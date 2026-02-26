# /// script
# dependencies = []
# ///
"""
Session registry — maps Discord threads and bot messages to Claude session IDs.

Persistent JSON-backed store that survives bot restarts. Used by:
- merlin_bot.py: look up session for a thread, register new thread→session
- discord_send.py: register message→session (for cron continuation)

File locking via fcntl ensures safe concurrent access.
"""

from __future__ import annotations

import fcntl
import json
import logging
from pathlib import Path

import paths

DATA_DIR = paths.data_dir() / "data"
REGISTRY_PATH = DATA_DIR / "session_registry.json"

logger = logging.getLogger("session-registry")


def _load() -> dict:
    """Load registry from disk. Returns empty structure if missing/corrupt."""
    if not REGISTRY_PATH.exists():
        return {"threads": {}, "messages": {}}
    try:
        with open(REGISTRY_PATH) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        if not isinstance(data, dict):
            return {"threads": {}, "messages": {}}
        data.setdefault("threads", {})
        data.setdefault("messages", {})
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not load session registry: %s", e)
        return {"threads": {}, "messages": {}}


def _save(data: dict) -> None:
    """Save registry to disk with exclusive file lock."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_PATH, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(data, f, indent=2)
            f.write("\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Thread → Session
# ---------------------------------------------------------------------------


def get_thread_session(thread_id: str) -> str | None:
    """Look up the Claude session ID for a Discord thread."""
    data = _load()
    return data["threads"].get(str(thread_id))


def set_thread_session(thread_id: str, session_id: str) -> None:
    """Register a thread → session mapping."""
    data = _load()
    data["threads"][str(thread_id)] = session_id
    _save(data)


# ---------------------------------------------------------------------------
# Message → Session
# ---------------------------------------------------------------------------


def get_message_session(message_id: str) -> str | None:
    """Look up the Claude session ID that produced a bot message."""
    data = _load()
    return data["messages"].get(str(message_id))


def set_message_session(message_id: str, session_id: str) -> None:
    """Register a bot message → session mapping."""
    data = _load()
    data["messages"][str(message_id)] = session_id
    _save(data)
