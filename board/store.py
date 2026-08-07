"""Durable per-session metadata for the Sessions board.

tmux carries the *live* signal (state, existence). This store carries what must
survive a window closing or a resume: the user's chosen name and manual order,
the pinned launch cwd, the family link, and tombstones for sessions that died
mid-work. Keyed by the stable ``@agent_sid`` minted at SessionStart.

Convention mirrors ``extensions.json``: one JSON file under ``~/.merlin/``
(``paths.data_dir() / "board.json"``), read-modify-written under a process lock.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import paths

_LOCK = threading.RLock()
_SCHEMA_VERSION = 1


def store_path() -> Path:
    """Location of the board metadata file (~/.merlin/board.json)."""
    return paths.data_dir() / "board.json"


@dataclass
class Session:
    """Durable record for one agent session, keyed by ``sid``.

    Everything tmux cannot keep for us across a window closing lives here. Live
    fields (``state``, ``live``, ``session``, ``window_id``) are refreshed from
    each sweep; ``name``/``order`` are user-owned and never overwritten by a
    sweep; ``cwd``/``parent``/``relation`` are pinned on first sight.
    """

    sid: str
    cwd: str = ""
    project: str = ""
    parent: str = ""
    relation: str = ""
    name: str | None = None  # user-chosen; None -> UI shows the auto-default
    order: float | None = None  # manual position; None -> fall back to first_seen
    first_seen: float = 0.0
    last_seen: float = 0.0
    state: str = "idle"  # last observed @agent_state
    live: bool = False  # present in the most recent sweep
    session: str = ""  # tmux session name (for focus), refreshed each sweep
    window_id: str = ""  # tmux window id (for focus), refreshed each sweep
    closed_at: float | None = None
    tombstone: bool = False  # died while busy -> kept until dismissed

    @staticmethod
    def from_dict(data: dict) -> Session:
        known = {f for f in Session.__dataclass_fields__}  # type: ignore[attr-defined]
        return Session(**{k: v for k, v in data.items() if k in known})


@dataclass
class Store:
    version: int = _SCHEMA_VERSION
    sessions: dict[str, Session] = field(default_factory=dict)


def load() -> Store:
    """Read the store. A missing or corrupt file yields an empty store rather
    than raising — the board is never worth crashing a request over."""
    path = store_path()
    if not path.exists():
        return Store()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return Store()
    if not isinstance(data, dict):
        return Store()
    raw_sessions = data.get("sessions")
    sessions: dict[str, Session] = {}
    if isinstance(raw_sessions, dict):
        for sid, rec in raw_sessions.items():
            if isinstance(rec, dict):
                with contextlib.suppress(TypeError):
                    sessions[sid] = Session.from_dict({**rec, "sid": sid})
    return Store(version=int(data.get("version", _SCHEMA_VERSION)), sessions=sessions)


def save(store: Store) -> None:
    """Atomic write (temp file + os.replace, 0600), so a crash mid-write never
    leaves a half-written store."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": store.version,
        "sessions": {sid: asdict(rec) for sid, rec in store.sessions.items()},
    }
    text = json.dumps(payload, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".board-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def transaction():
    """Load, yield the store for mutation, save on clean exit — all under the
    process lock so concurrent requests can't interleave read-modify-write."""
    with _LOCK:
        store = load()
        yield store
        save(store)
