"""Locked daily JSONL storage for Timeline activity events."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import paths
from pydantic import ValidationError

from .policy import HIDDEN_EVENT_KINDS
from .schema import ActivityEvent
from .writer import (
    MAX_RECORD_BYTES,
    RETENTION_DAYS,
    WriterError,
    acquire_exclusive,
    append_record,
    cleanup_retention,
)


MAX_READ_EVENTS = 10000
MAX_INDEXED_OPEN_SPANS = 10000
MAX_INDEXED_CLOSED_SPANS = 10000
_PARTITION_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_SPAN_INDEX_VERSION = 3


class ActivityStoreError(RuntimeError):
    """Strict-mode storage or validation failure."""


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors(
            include_url=False, include_context=False, include_input=False
        ):
            location = ".".join(str(item) for item in error.get("loc", ()))
            message = str(error.get("msg", "invalid value"))
            parts.append(f"{location}: {message}" if location else message)
        return "; ".join(parts) or "invalid activity event"
    return str(exc)


@dataclass(frozen=True)
class AppendResult:
    ok: bool
    duplicate: bool = False
    path: Path | None = None
    error: str | None = None


@dataclass(frozen=True)
class ReadResult:
    events: list[ActivityEvent]
    anomalies: int
    cursor: str
    last_modified_ns: int
    partial: bool


@dataclass(frozen=True)
class SpanContextResult:
    events: list[ActivityEvent]
    anomalies: int
    partial: bool
    known_spans: set[tuple[str, str]]


def activity_dir() -> Path:
    return paths.logs_dir() / "activity"


class ActivityStore:
    """Append and read bounded activity partitions without a server dependency."""

    def __init__(
        self, directory: Path | None = None, *, retention_days: int = RETENTION_DAYS
    ):
        self.directory = directory or activity_dir()
        self.retention_days = retention_days

    def append(
        self, value: ActivityEvent | dict, *, strict: bool = False
    ) -> AppendResult:
        try:
            event = (
                value
                if isinstance(value, ActivityEvent)
                else ActivityEvent.model_validate(value)
            )
            written = append_record(
                event.model_dump(mode="json", exclude_none=True),
                directory=self.directory,
                strict=True,
            )
            return AppendResult(
                ok=written.ok,
                duplicate=written.duplicate,
                path=written.path,
                error=written.error,
            )
        except (
            ActivityStoreError,
            OSError,
            TypeError,
            ValueError,
            ValidationError,
            WriterError,
        ) as exc:
            if strict:
                if isinstance(exc, ActivityStoreError):
                    raise
                raise ActivityStoreError(_safe_error(exc)) from exc
            return AppendResult(ok=False, error=_safe_error(exc))

    def cleanup_retention(self, *, now: datetime | None = None) -> list[Path]:
        try:
            return cleanup_retention(
                self.directory,
                now=now,
                retention_days=self.retention_days,
                force=True,
            )
        except OSError:
            return []

    def read_range(
        self,
        since: datetime,
        until: datetime,
        *,
        cursor: str | None = None,
        limit: int = 1000,
        exclude_kinds: frozenset[str] = frozenset(),
    ) -> ReadResult:
        since = _as_utc(since, "since")
        until = _as_utc(until, "until")
        if until < since:
            raise ActivityStoreError("until must not be before since")
        if not 1 <= limit <= MAX_READ_EVENTS:
            raise ActivityStoreError(f"limit must be between 1 and {MAX_READ_EVENTS}")

        offsets = _decode_cursor(cursor)
        next_offsets = dict(offsets)
        events: list[tuple[ActivityEvent, str, int]] = []
        seen_event_ids: set[str] = set()
        anomalies = 0
        last_modified_ns = 0
        partial = False

        partitions = self._partitions(since, until)
        for path in partitions:
            if len(events) >= limit:
                partial = True
                break
            try:
                stat = path.stat()
                last_modified_ns = max(last_modified_ns, stat.st_mtime_ns)
                offset = offsets.get(path.name, 0)
                if offset < 0 or offset > stat.st_size:
                    offset = 0
                    anomalies += 1
                with path.open("rb") as handle:
                    handle.seek(offset)
                    while len(events) < limit:
                        line_start = handle.tell()
                        line = handle.readline(MAX_RECORD_BYTES + 2)
                        if not line:
                            next_offsets[path.name] = handle.tell()
                            break
                        if len(line) > MAX_RECORD_BYTES or not line.endswith(b"\n"):
                            anomalies += 1
                            if not line.endswith(b"\n"):
                                next_offsets[path.name] = line_start
                                break
                            next_offsets[path.name] = handle.tell()
                            continue
                        next_offsets[path.name] = handle.tell()
                        try:
                            data = json.loads(line)
                            event = ActivityEvent.model_validate(data)
                        except (
                            json.JSONDecodeError,
                            UnicodeDecodeError,
                            ValidationError,
                            TypeError,
                            ValueError,
                        ):
                            anomalies += 1
                            continue
                        if event.event_id in seen_event_ids:
                            anomalies += 1
                            continue
                        seen_event_ids.add(event.event_id)
                        if event.kind in exclude_kinds:
                            continue
                        if since <= event.timestamp <= until:
                            events.append((event, path.name, line_start))
                    if len(events) >= limit:
                        partial = handle.readline(1) != b""
            except OSError:
                anomalies += 1

        events.sort(
            key=lambda item: (item[0].timestamp, item[1], item[2], item[0].event_id)
        )
        return ReadResult(
            events=[item[0] for item in events],
            anomalies=anomalies,
            cursor=_encode_cursor(next_offsets),
            last_modified_ns=last_modified_ns,
            partial=partial,
        )

    def read_span_context(
        self,
        since: datetime,
        until: datetime,
        *,
        limit: int = MAX_READ_EVENTS,
    ) -> SpanContextResult:
        """Return starts before the range for spans that cross into the range."""
        since = _as_utc(since, "since")
        until = _as_utc(until, "until")
        if until < since:
            raise ActivityStoreError("until must not be before since")
        if not 1 <= limit <= MAX_READ_EVENTS:
            raise ActivityStoreError(f"limit must be between 1 and {MAX_READ_EVENTS}")
        if not self.directory.exists():
            return SpanContextResult(
                events=[], anomalies=0, partial=False, known_spans=set()
            )

        lock_fd: int | None = None
        try:
            lock_path = self.directory / ".span-context.lock"
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(lock_fd, 0o600)
            acquire_exclusive(lock_fd)
            covered_partitions = {path.name for path in self._partitions(since, until)}
            state, anomalies = self._sync_span_index(covered_partitions)
        except OSError:
            return SpanContextResult(
                events=[], anomalies=1, partial=True, known_spans=set()
            )
        finally:
            if lock_fd is not None:
                os.close(lock_fd)

        candidates: list[ActivityEvent] = []
        known_spans: set[tuple[str, str]] = set()
        for value in state["open"].values():
            event = ActivityEvent.model_validate(value["start"])
            known_spans.add((event.trace_id, event.span_id or ""))
            if event.timestamp < since:
                candidates.append(event)
        for value in state["closed"].values():
            event = ActivityEvent.model_validate(value["start"])
            finish = ActivityEvent.model_validate(value["finish"])
            known_spans.add((event.trace_id, event.span_id or ""))
            if event.timestamp < since and finish.timestamp >= since:
                candidates.append(event)
                if finish.timestamp > until:
                    candidates.append(finish)
            elif since <= event.timestamp <= until and finish.timestamp > until:
                candidates.append(finish)

        candidates.sort(key=lambda event: (event.timestamp, event.event_id))
        partial = bool(state.get("partial"))
        if len(candidates) > limit:
            candidates = candidates[-limit:]
            partial = True
        return SpanContextResult(
            events=candidates,
            anomalies=anomalies,
            partial=partial,
            known_spans=known_spans,
        )

    def _sync_span_index(self, covered_partitions: set[str]) -> tuple[dict, int]:
        paths = self._all_partitions()
        index_path = self.directory / ".span-context.json"
        state, anomalies = _load_span_index(index_path)
        dirty = anomalies > 0
        names = {path.name for path in paths}
        recorded_names = set(state["files"])
        reset = False
        if recorded_names - names:
            reset = True
        for path in paths:
            metadata = state["files"].get(path.name)
            if metadata is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                reset = True
                break
            if metadata["inode"] != stat.st_ino or metadata["offset"] > stat.st_size:
                reset = True
                break
        if reset:
            state = _empty_span_index()
            anomalies += 1
            dirty = True

        for path in paths:
            metadata = state["files"].get(path.name, {"inode": 0, "offset": 0})
            try:
                stat = path.stat()
                offset = metadata["offset"] if metadata["inode"] == stat.st_ino else 0
                with path.open("rb") as handle:
                    handle.seek(offset)
                    while True:
                        line_start = handle.tell()
                        line = handle.readline(MAX_RECORD_BYTES + 2)
                        if not line:
                            offset = handle.tell()
                            break
                        if len(line) > MAX_RECORD_BYTES or not line.endswith(b"\n"):
                            if path.name not in covered_partitions:
                                anomalies += 1
                            if not line.endswith(b"\n"):
                                offset = line_start
                                break
                            offset = handle.tell()
                            continue
                        offset = handle.tell()
                        try:
                            event = ActivityEvent.model_validate(json.loads(line))
                        except (
                            json.JSONDecodeError,
                            UnicodeDecodeError,
                            ValidationError,
                            TypeError,
                            ValueError,
                        ):
                            if path.name not in covered_partitions:
                                anomalies += 1
                            continue
                        if event.kind in HIDDEN_EVENT_KINDS:
                            continue
                        if event.phase == "point":
                            continue
                        key = _span_key(event)
                        if event.phase == "start":
                            state["closed"].pop(key, None)
                            state["open"][key] = {
                                "partition": path.name,
                                "start": event.model_dump(
                                    mode="json", exclude_none=True
                                ),
                            }
                        else:
                            opened = state["open"].pop(key, None)
                            if opened is not None:
                                state["closed"][key] = {
                                    **opened,
                                    "finish_partition": path.name,
                                    "finish": event.model_dump(
                                        mode="json", exclude_none=True
                                    ),
                                }
                state["files"][path.name] = {
                    "inode": stat.st_ino,
                    "offset": offset,
                }
                if state["files"][path.name] != metadata:
                    dirty = True
            except OSError:
                if path.name not in covered_partitions:
                    anomalies += 1
                if not state["partial"]:
                    state["partial"] = True
                    dirty = True

        dirty = _cap_span_index(state, "open", MAX_INDEXED_OPEN_SPANS) or dirty
        dirty = _cap_span_index(state, "closed", MAX_INDEXED_CLOSED_SPANS) or dirty
        if dirty:
            try:
                _write_span_index(index_path, state)
            except OSError:
                anomalies += 1
                state["partial"] = True
        return state, anomalies

    def _partitions(self, since: datetime, until: datetime) -> list[Path]:
        return [
            path
            for path in self._all_partitions()
            if since.date().isoformat() <= path.stem <= until.date().isoformat()
        ]

    def _all_partitions(self) -> list[Path]:
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return []
        return sorted(
            (path for path in entries if _PARTITION_RE.fullmatch(path.name)),
            key=lambda path: path.name,
        )


def _empty_span_index() -> dict:
    return {
        "version": _SPAN_INDEX_VERSION,
        "files": {},
        "open": {},
        "closed": {},
        "partial": False,
    }


def _load_span_index(path: Path) -> tuple[dict, int]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return _empty_span_index(), 0
    except (OSError, json.JSONDecodeError):
        return _empty_span_index(), 1
    if isinstance(value, dict) and value.get("version") in {1, 2}:
        # Derived indexes are rebuilt on schema upgrades. An expected upgrade is
        # not malformed input and must not raise the public skipped count.
        return _empty_span_index(), 0
    if not isinstance(value, dict) or value.get("version") != _SPAN_INDEX_VERSION:
        return _empty_span_index(), 1
    if not all(isinstance(value.get(key), dict) for key in ("files", "open", "closed")):
        return _empty_span_index(), 1
    for metadata in value["files"].values():
        if not isinstance(metadata, dict) or not all(
            isinstance(metadata.get(key), int) and metadata[key] >= 0
            for key in ("inode", "offset")
        ):
            return _empty_span_index(), 1
    for group in ("open", "closed"):
        for item in value[group].values():
            if not isinstance(item, dict) or not isinstance(item.get("start"), dict):
                return _empty_span_index(), 1
            try:
                ActivityEvent.model_validate(item["start"])
                if group == "closed":
                    ActivityEvent.model_validate(item["finish"])
            except (KeyError, ValidationError, TypeError, ValueError):
                return _empty_span_index(), 1
    value["partial"] = value.get("partial") is True
    return value, 0


def _span_key(event: ActivityEvent) -> str:
    value = f"{event.trace_id}\0{event.span_id}".encode()
    return hashlib.sha256(value).hexdigest()


def _cap_span_index(state: dict, group: str, limit: int) -> bool:
    values = state[group]
    if len(values) <= limit:
        return False
    ordered = sorted(
        values,
        key=lambda key: (
            (values[key].get("finish") or {}).get("timestamp")
            or values[key]["start"]["timestamp"]
        ),
    )
    for key in ordered[: len(values) - limit]:
        del values[key]
    state["partial"] = True
    return True


def _write_span_index(path: Path, state: dict) -> None:
    rendered = json.dumps(state, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=".span-context-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(rendered)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _as_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ActivityStoreError(f"{name} must include a UTC offset")
    return value.astimezone(timezone.utc)


def _encode_cursor(offsets: dict[str, int]) -> str:
    payload = json.dumps({"v": 1, "files": offsets}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> dict[str, int]:
    if not cursor:
        return {}
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if payload.get("v") != 1 or not isinstance(payload.get("files"), dict):
            raise ValueError("unsupported cursor")
        offsets: dict[str, int] = {}
        for name, offset in payload["files"].items():
            if not _PARTITION_RE.fullmatch(name) or not isinstance(offset, int):
                raise ValueError("invalid cursor entry")
            offsets[name] = offset
        return offsets
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ActivityStoreError("invalid activity cursor") from exc
