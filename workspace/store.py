"""The workspace files and their atomic writes.

Under ``paths.workspace_dir()``:

- ``latest.json``: the continuous snapshot, overwritten by the sweep whenever
  the workspace changes.
- ``history/<id>.json``: a copy of every snapshot the sweep writes, thinned
  (``thin``): everything of the last two hours, then the newest of each clock
  hour up to seven days, nothing older.
- ``crashes/<id>.json``: a crash point, the snapshot a dead server was last
  seen with plus a ``crash`` object (``reason``, ``detected_at``). Kept thirty
  days, never thinned.
- ``pending-restore.json``: the restore offer, a full copy of the crash point
  plus its ``id``, deleted by Restore or Dismiss.

Ids are the snapshot's ``saved_at`` in local time, ``YYYYMMDD-HHMMSS``,
suffixed ``-2``, ``-3`` on a collision. History and crash ids share one
namespace, so ``show`` and ``restore`` take either without a prefix.

Every write goes temp-then-rename with an fsync, because a power cut mid-write
is exactly the case this feature exists for. A file that fails to parse or has
an unknown shape reads as absent.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

import paths

from .snapshot import SNAPSHOT_VERSION

HISTORY_KEEP_ALL = 2 * 3600
HISTORY_KEEP_HOURLY = 7 * 86400
CRASH_KEEP = 30 * 86400

ID_RE = re.compile(r"^(\d{8}-\d{6})(?:-(\d+))?$")
_ID_FORMAT = "%Y%m%d-%H%M%S"

# A timestamp the store accepts: a finite number from the epoch to a date
# ``time.localtime`` can render on every platform (year 2242). Anything else
# in ``saved_at`` makes the file read as absent, so that no id or retention
# computation can raise on a foreign or torn file.
_TIME_MAX = 2**33


def valid_time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # The bound first: JSON ints are unbounded and ``isfinite`` converts to
    # a float, which overflows on a big enough one.
    if isinstance(value, int):
        return float(value) if 0 <= value <= _TIME_MAX else None
    if not math.isfinite(value) or not 0 <= value <= _TIME_MAX:
        return None
    return value


def latest_path() -> Path:
    return paths.workspace_dir() / "latest.json"


def pending_path() -> Path:
    return paths.workspace_dir() / "pending-restore.json"


def history_dir() -> Path:
    return paths.workspace_dir() / "history"


def crashes_dir() -> Path:
    return paths.workspace_dir() / "crashes"


def read_snapshot(path: Path) -> dict | None:
    """A valid snapshot from ``path``, or None (absent, torn, unknown shape).

    ``saved_at`` must be a valid time: every snapshot Merlin ever wrote has
    one (``build_snapshot`` always sets it), so a file without it, or with
    one that is not a finite number in range, is foreign and reads as absent
    rather than making a later ``localtime`` of it raise.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
        return None
    if valid_time(data.get("saved_at")) is None:
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


def server_of(snapshot: dict) -> tuple[int, int] | None:
    """The ``(pid, start_time)`` of the server a snapshot was taken from, or
    None when the snapshot does not carry it (written before the field
    existed) or carries something malformed."""
    server = snapshot.get("server")
    if not isinstance(server, dict):
        return None
    pid, start = server.get("pid"), server.get("start_time")
    if isinstance(pid, bool) or isinstance(start, bool):
        return None
    if not isinstance(pid, int) or not isinstance(start, int):
        return None
    return pid, start


def crash_of(snapshot: dict) -> dict | None:
    """The ``crash`` object of a crash point or an offer, or None when the
    snapshot has none or it is malformed (a reason that is not a string, a
    ``detected_at`` that is not a valid time)."""
    crash = snapshot.get("crash")
    if not isinstance(crash, dict) or not isinstance(crash.get("reason"), str):
        return None
    if valid_time(crash.get("detected_at")) is None:
        return None
    return crash


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


# --- ids --------------------------------------------------------------------


def id_time(entry_id: str) -> float | None:
    """The local time an id names, as an epoch, or None for a foreign name."""
    m = ID_RE.match(entry_id)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), _ID_FORMAT))
    except (ValueError, OverflowError):
        return None


def _id_key(entry_id: str) -> tuple[float, int]:
    """Sort key: the time, then the collision suffix (a later write)."""
    m = ID_RE.match(entry_id)
    suffix = int(m.group(2)) if m and m.group(2) else 1
    return id_time(entry_id) or 0.0, suffix


def _entries(directory: Path) -> list[Path]:
    """The ``<id>.json`` files of a directory. A foreign name is not an entry
    and is never touched."""
    try:
        return [
            p
            for p in directory.iterdir()
            if p.suffix == ".json" and ID_RE.match(p.stem)
        ]
    except OSError:
        return []


def new_id(saved_at: float) -> str:
    """An unused id for a snapshot saved at ``saved_at``, across history and
    crashes together."""
    base = time.strftime(_ID_FORMAT, time.localtime(saved_at))
    taken = {p.stem for d in (history_dir(), crashes_dir()) for p in _entries(d)}
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def entry_path(entry_id: str) -> Path | None:
    """The file behind an id, a crash point or a history entry, or None."""
    if not ID_RE.match(entry_id):
        return None
    for directory in (crashes_dir(), history_dir()):
        path = directory / f"{entry_id}.json"
        if path.exists():
            return path
    return None


# --- history and crash points ----------------------------------------------


def write_history(snapshot: dict) -> str:
    """Keep a copy of a snapshot the sweep just wrote. Returns its id."""
    entry_id = new_id(snapshot["saved_at"])
    write_snapshot(history_dir() / f"{entry_id}.json", snapshot)
    return entry_id


def write_crash(snapshot: dict, reason: str, detected_at: float) -> tuple[str, dict]:
    """Freeze a snapshot as a crash point. Returns its id and the frozen
    snapshot, the one with its ``crash`` object."""
    frozen = {
        **snapshot,
        "crash": {"reason": reason, "detected_at": float(detected_at)},
    }
    entry_id = new_id(snapshot["saved_at"])
    write_snapshot(crashes_dir() / f"{entry_id}.json", frozen)
    return entry_id, frozen


def _detected_at(path: Path) -> float | None:
    """When the crash point at ``path`` was detected, from its file."""
    data = read_snapshot(path)
    crash = crash_of(data) if data else None
    return float(crash["detected_at"]) if crash else None


def thin(now: float | None = None) -> list[Path]:
    """Apply the retention. Returns what was removed.

    History, from the listing alone: every entry younger than
    ``HISTORY_KEEP_ALL`` stays; up to ``HISTORY_KEEP_HOURLY`` the newest entry
    of each clock hour stays; older entries go. The age is the id's own time,
    the snapshot's save time.

    Crash points go ``CRASH_KEEP`` after their detection, not their save
    time: a snapshot can be months old when the server it was taken from
    dies (Merlin stopped for a while, nothing changed for days). The id is
    only the cheap filter: a detection is never earlier than the save time,
    so a crash point whose id is young is never read. One whose file has
    lost its crash object falls back to the id's time.
    """
    now = time.time() if now is None else now
    doomed: list[Path] = []
    newest_of_hour: dict[str, Path] = {}
    for path in _entries(history_dir()):
        ts = id_time(path.stem)
        if ts is None:
            continue
        age = now - ts
        if age < HISTORY_KEEP_ALL:
            continue
        if age >= HISTORY_KEEP_HOURLY:
            doomed.append(path)
            continue
        hour = path.stem[:11]  # YYYYMMDD-HH, the local clock hour
        best = newest_of_hour.get(hour)
        if best is None:
            newest_of_hour[hour] = path
        elif _id_key(path.stem) > _id_key(best.stem):
            doomed.append(best)
            newest_of_hour[hour] = path
        else:
            doomed.append(path)
    for path in _entries(crashes_dir()):
        ts = id_time(path.stem)
        if ts is None or now - ts < CRASH_KEEP:
            continue
        detected = _detected_at(path)
        if now - (ts if detected is None else detected) >= CRASH_KEEP:
            doomed.append(path)
    removed = []
    for path in doomed:
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed
