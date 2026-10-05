"""The two workspace files and their atomic writes.

``latest.json`` is the continuous snapshot, overwritten by the sweep whenever
the workspace changes. ``pending-restore.json`` is the restore offer, frozen at
server start from ``latest.json`` when sessions it holds are missing from tmux,
and deleted by Restore or Dismiss. Freezing it before the sweep's first write
is what keeps the restore point alive: right after a reboot the first sweep
would otherwise overwrite ``latest.json`` with the empty workspace.

Both are written temp-then-rename with an fsync, because a power cut mid-write
is exactly the case this feature exists for. A file that fails to parse or has
an unknown shape reads as absent.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

import paths

from .snapshot import SNAPSHOT_VERSION


def latest_path() -> Path:
    return paths.workspace_dir() / "latest.json"


def pending_path() -> Path:
    return paths.workspace_dir() / "pending-restore.json"


def read_snapshot(path: Path) -> dict | None:
    """A valid snapshot from ``path``, or None (absent, torn, unknown shape)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
        return None
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        return None
    for s in sessions:
        if not (
            isinstance(s, dict)
            and isinstance(s.get("name"), str)
            and isinstance(s.get("windows"), list)
        ):
            return None
    return data


def write_snapshot(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` atomically and durably."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ws-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # Make the rename itself durable.
    with contextlib.suppress(OSError):
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def delete_pending() -> None:
    with contextlib.suppress(FileNotFoundError):
        pending_path().unlink()
