"""Small locked sidecar for provider events that omit matching span ids."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from .writer import acquire_exclusive, default_activity_dir


PENDING_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_PENDING_KEYS = 4096


def _paths(directory: Path | None = None) -> tuple[Path, Path]:
    root = directory or default_activity_dir()
    return root / ".pending.lock", root / ".pending.json"


def _with_state(directory: Path | None, callback):
    lock_path, state_path = _paths(directory)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        acquire_exclusive(fd)
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        now = time.time()
        changed = False
        for key, entry in list(state.items()):
            if (
                isinstance(entry, list)
                and len(entry) == 1
                and isinstance(entry[0], str)
            ):
                state[key] = {
                    "type": "pending",
                    "span": entry[0],
                    "created_at": now,
                }
                changed = True
                continue
            created = entry.get("created_at") if isinstance(entry, dict) else None
            if (
                not isinstance(created, int | float)
                or now - created > PENDING_TTL_SECONDS
            ):
                state.pop(key, None)
                changed = True
        result, callback_changed = callback(state, now)
        changed = changed or callback_changed
        if len(state) > MAX_PENDING_KEYS:
            oldest = sorted(
                state,
                key=lambda key: state[key].get("created_at", 0),
            )[: len(state) - MAX_PENDING_KEYS]
            for key in oldest:
                state.pop(key, None)
            changed = True
        if changed:
            rendered = json.dumps(state, separators=(",", ":")) + "\n"
            tmp_fd, temporary = tempfile.mkstemp(
                dir=lock_path.parent, prefix=".pending-", suffix=".tmp"
            )
            try:
                with os.fdopen(tmp_fd, "w") as handle:
                    handle.write(rendered)
                os.chmod(temporary, 0o600)
                os.replace(temporary, state_path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        return result
    finally:
        os.close(fd)


def remember_pending(key: str, *, directory: Path | None = None) -> tuple[str, bool]:
    """Return one pending span for a scope, creating it only when absent."""

    def update(state: dict, now: float):
        entry = state.get(key)
        if (
            isinstance(entry, dict)
            and entry.get("type") == "pending"
            and isinstance(entry.get("span"), str)
        ):
            return (entry["span"], False), False
        span_id = f"pending:{uuid.uuid4()}"
        state[key] = {"type": "pending", "span": span_id, "created_at": now}
        return (span_id, True), True

    return _with_state(directory, update)


def take_pending(key: str, *, directory: Path | None = None) -> str | None:
    """Take one exact pending span and remove any unusable entry for the key."""

    def update(state: dict, _now: float):
        entry = state.get(key)
        span_id = (
            entry.get("span")
            if isinstance(entry, dict) and entry.get("type") == "pending"
            else None
        )
        if key not in state:
            return None, False
        state.pop(key, None)
        return (span_id if isinstance(span_id, str) else None), True

    return _with_state(directory, update)


def consume_once(key: str, *, directory: Path | None = None) -> bool:
    """Return true once for a durable correlation key within the sidecar TTL."""

    def update(state: dict, now: float):
        entry = state.get(key)
        if isinstance(entry, dict) and entry.get("type") == "once":
            return False, False
        state[key] = {"type": "once", "created_at": now}
        return True, True

    return _with_state(directory, update)


def clear_pending(*prefixes: str, directory: Path | None = None) -> int:
    """Clear pending lifecycle keys for a completed or restarted provider scope."""

    def update(state: dict, _now: float):
        removed = 0
        for key, entry in list(state.items()):
            if not any(key.startswith(prefix) for prefix in prefixes):
                continue
            if not isinstance(entry, dict) or entry.get("type") != "pending":
                continue
            state.pop(key, None)
            removed += 1
        return removed, removed > 0

    return _with_state(directory, update)
