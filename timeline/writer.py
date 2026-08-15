"""Dependency-free locked writer shared by hooks, CLI, and the typed store."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .protocol import ProtocolError, validate_record


MAX_RECORD_BYTES = 8192
MAX_DAILY_BYTES = 64 * 1024 * 1024
RETENTION_DAYS = 90
LOCK_TIMEOUT_SECONDS = 0.25
LOCK_POLL_SECONDS = 0.005
_PARTITION_SUFFIX = ".jsonl"


class WriterError(RuntimeError):
    pass


class LockTimeoutError(TimeoutError):
    pass


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    duplicate: bool = False
    path: Path | None = None
    error: str | None = None


def default_activity_dir() -> Path:
    home = Path(os.environ.get("MERLIN_HOME") or Path.home() / ".merlin").expanduser()
    return home.resolve() / "logs" / "activity"


def acquire_exclusive(fd: int, *, timeout: float | None = None) -> None:
    """Acquire an exclusive flock within a bounded fail-open budget."""
    budget = LOCK_TIMEOUT_SECONDS if timeout is None else max(0.0, timeout)
    deadline = time.monotonic() + budget
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise LockTimeoutError("activity lock timed out") from exc
            time.sleep(min(LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def _atomic_private_text(path: Path, text: str, *, prefix: str) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def cleanup_retention(
    directory: Path,
    *,
    now: datetime | None = None,
    retention_days: int = RETENTION_DAYS,
    force: bool = False,
) -> list[Path]:
    """Remove expired partitions, scanning at most once per UTC day by default."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = current.date()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = directory / ".retention.lock"
    stamp_path = directory / ".retention-day"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        acquire_exclusive(lock_fd)
        if not force:
            try:
                if stamp_path.read_text().strip() == today.isoformat():
                    return []
            except OSError:
                pass
        cutoff = today - timedelta(days=retention_days)
        removed: list[Path] = []
        for path in directory.iterdir():
            if not path.name.endswith(_PARTITION_SUFFIX):
                continue
            try:
                day = date.fromisoformat(path.name.removesuffix(_PARTITION_SUFFIX))
            except ValueError:
                continue
            if day >= cutoff or day == today:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(path)
            try:
                shutil.rmtree(_index_root(directory, path.stem))
            except OSError:
                pass
        _atomic_private_text(stamp_path, today.isoformat() + "\n", prefix=".retention-")
        return removed
    finally:
        os.close(lock_fd)


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, WriterError) and str(exc) == "daily activity partition is full":
        return "daily-cap"
    if isinstance(exc, LockTimeoutError):
        return "lock-timeout"
    if isinstance(exc, ProtocolError | TypeError | ValueError):
        return "normalization-error"
    return "write-error"


def record_capture_failure(directory: Path, exc: BaseException) -> None:
    """Record a bounded current-day drop count without serializing error content."""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        lock_path = directory / ".capture-health.lock"
        state_path = directory / ".capture-health.json"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(lock_fd, 0o600)
            acquire_exclusive(lock_fd)
            today = datetime.now(timezone.utc).date().isoformat()
            try:
                state = json.loads(state_path.read_text())
            except (OSError, json.JSONDecodeError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            count = state.get("dropped", 0) if state.get("day") == today else 0
            count = count if isinstance(count, int) and count >= 0 else 0
            rendered = json.dumps(
                {
                    "version": 1,
                    "day": today,
                    "dropped": min(count + 1, 2**31 - 1),
                    "last_error": _failure_code(exc),
                    "updated_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
                separators=(",", ":"),
            )
            _atomic_private_text(state_path, rendered + "\n", prefix=".health-")
        finally:
            os.close(lock_fd)
    except OSError:
        pass


def read_capture_health(directory: Path) -> dict[str, object]:
    """Return the bounded current-day capture-health state."""
    try:
        value = json.loads((directory / ".capture-health.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"dropped": 0}
    today = datetime.now(timezone.utc).date().isoformat()
    if not isinstance(value, dict) or value.get("day") != today:
        return {"dropped": 0}
    dropped = value.get("dropped")
    if not isinstance(dropped, int) or dropped < 0:
        return {"dropped": 0}
    return {
        "dropped": dropped,
        "last_error": value.get("last_error"),
        "updated_at": value.get("updated_at"),
    }


def _index_root(directory: Path, day: str) -> Path:
    return directory / ".event-ids" / day


def _ensure_index_root(root: Path) -> None:
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root.parent, 0o700)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)


def _append_index_id(root: Path, event_id: str) -> None:
    _ensure_index_root(root)
    path = root / event_id[:2]
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (event_id + "\n").encode())
    finally:
        os.close(fd)


def _sync_event_index(fd: int, root: Path) -> None:
    """Index only partition bytes not covered by the durable offset marker."""
    size = os.fstat(fd).st_size
    marker = root / ".offset"
    try:
        offset = int(marker.read_text().strip())
    except (OSError, ValueError):
        offset = 0
    if offset < 0 or offset > size:
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        offset = 0
    if offset < size:
        os.lseek(fd, offset, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb", closefd=True) as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                event_id = value.get("event_id") if isinstance(value, dict) else None
                if isinstance(event_id, str):
                    _append_index_id(root, event_id)
    _ensure_index_root(root)
    _atomic_private_text(marker, f"{size}\n", prefix=".offset-")


def _contains_event_id(fd: int, directory: Path, day: str, event_id: str) -> bool:
    root = _index_root(directory, day)
    _sync_event_index(fd, root)
    shard = root / event_id[:2]
    if not shard.exists():
        return False
    with shard.open("r") as handle:
        return any(line.rstrip("\n") == event_id for line in handle)


def _record_indexed_append(fd: int, directory: Path, day: str, event_id: str) -> None:
    root = _index_root(directory, day)
    _append_index_id(root, event_id)
    _atomic_private_text(
        root / ".offset",
        f"{os.fstat(fd).st_size}\n",
        prefix=".offset-",
    )


def append_record(
    record: dict,
    *,
    directory: Path | None = None,
    strict: bool = False,
) -> WriteResult:
    target: Path | None = None
    try:
        checked = validate_record(record)
        encoded = (
            json.dumps(checked, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode()
        if len(encoded) > MAX_RECORD_BYTES:
            raise WriterError(f"event exceeds {MAX_RECORD_BYTES} bytes")
        target = directory or default_activity_dir()
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        day = checked["timestamp"][:10]
        path = target / f"{day}.jsonl"
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            acquire_exclusive(fd)
            duplicate = _contains_event_id(fd, target, day, checked["event_id"])
            if not duplicate:
                if os.fstat(fd).st_size + len(encoded) > MAX_DAILY_BYTES:
                    raise WriterError("daily activity partition is full")
                if os.write(fd, encoded) != len(encoded):
                    raise WriterError("short activity append")
                try:
                    _record_indexed_append(fd, target, day, checked["event_id"])
                except OSError:
                    pass
        finally:
            os.close(fd)
        try:
            cleanup_retention(target)
        except OSError:
            pass
        return WriteResult(ok=True, duplicate=duplicate, path=path)
    except (OSError, ProtocolError, TypeError, ValueError, WriterError) as exc:
        if target is not None and isinstance(exc, OSError | WriterError):
            record_capture_failure(target, exc)
        if strict:
            if isinstance(exc, WriterError):
                raise
            raise WriterError(str(exc)) from exc
        return WriteResult(ok=False, error=str(exc))
