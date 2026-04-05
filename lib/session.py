"""
Session manager — JSONL-based conversation history.

Each session is a file at ~/.merlin/sessions/<session_id>.jsonl containing
one JSON object per line: a header followed by conversation turns.

Usage:
    from lib.session import load_session, append_turn, create_session

    # Create a new session
    create_session("abc-123", engine="claude-code", model="claude-opus-4-6")

    # Append turns
    append_turn("abc-123", {"role": "user", "content": "Hello"})
    append_turn("abc-123", {"role": "assistant", "content": "Hi there"})

    # Load history
    history = load_session("abc-123")
"""

from __future__ import annotations

import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import paths

logger = logging.getLogger("merlin.session")

# Version of the session format
SESSION_VERSION = 1


def get_session_path(session_id: str) -> Path:
    """Return the path for a session file."""
    return paths.sessions_dir() / f"{session_id}.jsonl"


def session_exists(session_id: str) -> bool:
    """Check if a session file exists."""
    return get_session_path(session_id).exists()


def create_session(
    session_id: str,
    *,
    engine: str = "unknown",
    model: str | None = None,
) -> Path:
    """Create a new session file with a header line.

    Returns the path to the session file. If the session already exists,
    returns the existing path without overwriting.
    """
    path = get_session_path(session_id)
    if path.exists():
        return path

    path.parent.mkdir(parents=True, exist_ok=True)

    header = {
        "v": SESSION_VERSION,
        "session_id": session_id,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "engine": engine,
        "model": model,
    }

    with open(path, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(header, separators=(",", ":")) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    return path


def append_turn(session_id: str, turn: dict) -> None:
    """Append a turn to a session file.

    If the session doesn't exist, creates it first with a minimal header.
    Adds a timestamp if not present.
    """
    path = get_session_path(session_id)

    if not path.exists():
        create_session(session_id)

    # Ensure timestamp
    if "ts" not in turn:
        turn = {**turn, "ts": datetime.now(tz=timezone.utc).isoformat()}

    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(turn, separators=(",", ":")) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_session(session_id: str) -> list[dict]:
    """Load all turns from a session file.

    Returns a list of turn dicts (excluding the header).
    Returns empty list if the session doesn't exist.
    """
    path = get_session_path(session_id)
    if not path.exists():
        return []

    turns: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping invalid JSON line in %s", path)
                continue
            # Skip header (has "v" key)
            if "v" in obj:
                continue
            turns.append(obj)
    except OSError as e:
        logger.warning("Could not read session %s: %s", session_id, e)

    return turns


def load_session_header(session_id: str) -> dict | None:
    """Load just the header from a session file. Returns None if not found."""
    path = get_session_path(session_id)
    if not path.exists():
        return None

    try:
        first_line = path.read_text(encoding="utf-8").split("\n", 1)[0].strip()
        if first_line:
            obj = json.loads(first_line)
            if "v" in obj:
                return obj
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read session header %s: %s", session_id, e)

    return None


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def _turn_tokens(turn: dict) -> int:
    """Estimate tokens for a single turn."""
    content = turn.get("content", "")
    output = turn.get("output", "")
    input_str = json.dumps(turn.get("input", "")) if "input" in turn else ""
    return _estimate_tokens(content + output + input_str)


def compact_history(
    history: list[dict],
    max_tokens: int,
    keep_recent: int = 20,
) -> list[dict]:
    """Compact history by dropping oldest turns if over token limit.

    Keeps:
    - The system turn (first turn if role == "system")
    - The last `keep_recent` turns
    - Inserts a compaction marker where turns were dropped

    Returns the compacted history. If no compaction needed, returns
    the original list unchanged.
    """
    if not history:
        return history

    total = sum(_turn_tokens(t) for t in history)
    if total <= max_tokens:
        return history

    # Separate system prompt (if first turn is system)
    system_turn = None
    rest = history
    if history[0].get("role") == "system":
        system_turn = history[0]
        rest = history[1:]

    # Keep last N turns
    if len(rest) <= keep_recent:
        return history  # Nothing to drop

    kept = rest[-keep_recent:]
    dropped_count = len(rest) - keep_recent

    compaction_marker = {
        "role": "compaction",
        "dropped": dropped_count,
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }

    result: list[dict] = []
    if system_turn:
        result.append(system_turn)
    result.append(compaction_marker)
    result.extend(kept)

    return result
