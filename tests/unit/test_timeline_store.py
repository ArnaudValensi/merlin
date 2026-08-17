"""Typed schema, concurrent append, retention, and cursor tests."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from timeline import protocol, schema, writer
from timeline.protocol import ProtocolError
from timeline.schema import ActivityEvent
from timeline.store import ActivityStore, ActivityStoreError
from timeline.writer import WriterError, append_record, read_capture_health


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def event(
    *,
    event_id: str | None = None,
    timestamp: datetime = NOW,
    phase: str = "point",
    kind: str = "human.prompt",
    span_id: str | None = None,
    status: str = "ok",
    **extra,
) -> dict:
    value = {
        "schema_version": 1,
        "event_id": event_id or str(uuid.uuid4()),
        "timestamp": timestamp.isoformat()
        if isinstance(timestamp, datetime)
        else timestamp,
        "phase": phase,
        "kind": kind,
        "trace_id": "trace-1",
        "span_id": span_id,
        "actor": {"type": "human", "id": "human", "label": "Human"},
        "context": {},
        "status": status,
        "name": "Prompt submitted",
        "attributes": {},
    }
    value.update(extra)
    return value


def _append_worker(args: tuple[str, int]) -> tuple[bool, bool]:
    directory, index = args
    result = ActivityStore(Path(directory)).append(
        event(
            event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"timeline-{index}")),
            kind="tool.call",
            phase="start",
            span_id=f"tool-{index}",
            status="running",
        ),
        strict=True,
    )
    return result.ok, result.duplicate


def test_schema_accepts_unknown_kind_fields_and_context_metadata():
    value = event(kind="future.provider_event", future_top={"version": 2})
    value["actor"]["future_role"] = "new"
    value["context"]["future_context"] = "kept"
    parsed = ActivityEvent.model_validate(value)
    dumped = parsed.model_dump()
    assert dumped["future_top"] == {"version": 2}
    assert dumped["actor"]["future_role"] == "new"
    assert dumped["context"]["future_context"] == "kept"


@pytest.mark.parametrize(
    "container",
    [
        lambda value: value.update(prompt="must not land"),
        lambda value: value["actor"].update(prompt="must not land"),
    ],
)
def test_unknown_forward_fields_still_enforce_privacy_at_every_level(
    tmp_path, container
):
    value = event()
    container(value)
    with pytest.raises(ValidationError, match="content-bearing"):
        ActivityEvent.model_validate(value)
    with pytest.raises(WriterError, match="content-bearing"):
        append_record(value, directory=tmp_path, strict=True)


def test_dependency_free_and_pydantic_protocol_limits_cannot_drift():
    assert schema.SCHEMA_VERSION == protocol.SCHEMA_VERSION
    assert schema.MAX_NAME_BYTES == protocol.MAX_NAME_BYTES
    assert schema.MAX_ATTRIBUTES_BYTES == protocol.MAX_ATTRIBUTES_BYTES
    assert schema.MAX_CONTEXT_BYTES == protocol.MAX_CONTEXT_BYTES
    assert schema.MAX_ATTRIBUTE_DEPTH == protocol.MAX_ATTRIBUTE_DEPTH
    assert schema.MAX_ATTRIBUTE_ITEMS == protocol.MAX_ATTRIBUTE_ITEMS
    assert schema._BLOCKED_ATTRIBUTE_KEYS == protocol.BLOCKED_ATTRIBUTE_KEYS


def test_dependency_free_and_pydantic_protocol_reject_scalar_attributes():
    value = event(attributes="not-an-object")
    with pytest.raises(ValidationError):
        ActivityEvent.model_validate(value)
    with pytest.raises(ProtocolError, match="attributes must be an object"):
        protocol.validate_record(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "not-a-uuid"},
        {"timestamp": "2026-08-15T12:00:00"},
        {"kind": "Not Dotted"},
        {"phase": "start", "span_id": None, "status": "running"},
        {"phase": "point", "span_id": "unexpected"},
        {"phase": "finish", "span_id": "span", "status": "running"},
        {"attributes": {"prompt": "must not land"}},
    ],
)
def test_schema_rejects_invalid_or_content_bearing_records(changes):
    with pytest.raises(ValidationError):
        ActivityEvent.model_validate(event(**changes))


@pytest.mark.parametrize("field", ["prompt", "tool_input", "message", "stdout"])
def test_schema_and_writer_reject_content_bearing_context(tmp_path, field):
    value = event(context={field: "sensitive text"})
    with pytest.raises(ValidationError, match="content-bearing"):
        ActivityEvent.model_validate(value)
    with pytest.raises(WriterError, match="content-bearing") as raised:
        append_record(value, directory=tmp_path, strict=True)
    assert isinstance(raised.value.__cause__, ProtocolError)


def test_append_is_idempotent_and_enforces_private_permissions(tmp_path):
    store = ActivityStore(tmp_path / "activity")
    value = event()
    first = store.append(value, strict=True)
    second = store.append(value, strict=True)
    assert first.ok and not first.duplicate
    assert second.ok and second.duplicate
    assert len(first.path.read_text().splitlines()) == 1
    assert (store.directory.stat().st_mode & 0o777) == 0o700
    assert (first.path.stat().st_mode & 0o777) == 0o600
    index_root = store.directory / ".event-ids" / "2026-08-15"
    assert index_root.parent.stat().st_mode & 0o777 == 0o700
    assert index_root.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in index_root.iterdir())


def test_event_id_index_recovers_unindexed_partition_tail(tmp_path):
    directory = tmp_path / "activity"
    first = event()
    append_record(first, directory=directory, strict=True)
    unindexed = event()
    path = directory / "2026-08-15.jsonl"
    with path.open("a") as handle:
        handle.write(json.dumps(unindexed) + "\n")

    duplicate = append_record(unindexed, directory=directory, strict=True)

    assert duplicate.duplicate is True
    assert path.read_text().count(unindexed["event_id"]) == 1


def test_missing_event_id_shard_is_a_definite_miss_not_a_partition_scan(
    tmp_path, monkeypatch
):
    directory = tmp_path / "activity"
    first = event()
    append_record(first, directory=directory, strict=True)
    candidate = event()
    while candidate["event_id"][:2] == first["event_id"][:2]:
        candidate = event()

    monkeypatch.setattr(
        writer.os,
        "lseek",
        lambda *_args: pytest.fail("a synchronized missing shard must not scan"),
    )

    result = append_record(candidate, directory=directory, strict=True)

    assert result.ok is True


def test_unavailable_event_id_index_drops_write_instead_of_scanning_partition(
    tmp_path, monkeypatch
):
    directory = tmp_path / "activity"
    append_record(event(), directory=directory, strict=True)
    partition = directory / "2026-08-15.jsonl"
    before = partition.read_bytes()
    monkeypatch.setattr(
        writer,
        "_sync_event_index",
        lambda *_args: (_ for _ in ()).throw(OSError("index unavailable")),
    )

    result = append_record(event(), directory=directory, strict=False)

    assert result.ok is False
    assert partition.read_bytes() == before
    assert read_capture_health(directory)["last_error"] == "write-error"


def test_partition_lock_contention_fails_open_within_bounded_budget(
    tmp_path, monkeypatch
):
    directory = tmp_path / "activity"
    append_record(event(), directory=directory, strict=True)
    partition = directory / "2026-08-15.jsonl"
    lock_fd = os.open(partition, os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    monkeypatch.setattr(writer, "LOCK_TIMEOUT_SECONDS", 0.02)
    started = time.monotonic()
    try:
        result = append_record(event(), directory=directory, strict=False)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert time.monotonic() - started < 0.5
    assert result.ok is False
    assert read_capture_health(directory)["last_error"] == "lock-timeout"


def test_many_processes_append_complete_unique_lines(tmp_path):
    directory = tmp_path / "activity"
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=8) as pool:
        results = pool.map(_append_worker, [(str(directory), i) for i in range(160)])
    records = [
        json.loads(line)
        for line in (directory / "2026-08-15.jsonl").read_text().splitlines()
    ]
    assert results == [(True, False)] * 160
    assert len(records) == 160
    assert len({record["event_id"] for record in records}) == 160


def test_daily_partition_boundary_and_stable_read_order(tmp_path):
    store = ActivityStore(tmp_path / "activity")
    later = event(timestamp=NOW + timedelta(days=1), name="Later")
    same_b = event(event_id="00000000-0000-4000-8000-000000000002", name="B")
    same_a = event(event_id="00000000-0000-4000-8000-000000000001", name="A")
    for value in (later, same_b, same_a):
        store.append(value, strict=True)
    result = store.read_range(NOW - timedelta(minutes=1), NOW + timedelta(days=2))
    assert [item.name for item in result.events] == ["B", "A", "Later"]
    assert {path.name for path in store.directory.glob("*.jsonl")} == {
        "2026-08-15.jsonl",
        "2026-08-16.jsonl",
    }


def test_cursor_reads_only_new_complete_records_and_recovers_truncated_tail(tmp_path):
    store = ActivityStore(tmp_path / "activity")
    store.append(event(name="First"), strict=True)
    first = store.read_range(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert [item.name for item in first.events] == ["First"]

    path = store.directory / "2026-08-15.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"event_id":"truncated"')
    cut = store.read_range(
        NOW - timedelta(hours=1), NOW + timedelta(hours=1), cursor=first.cursor
    )
    assert cut.events == []
    assert cut.anomalies == 1

    path.write_bytes(path.read_bytes()[: -len(b'{"event_id":"truncated"')])
    store.append(event(name="Second"), strict=True)
    second = store.read_range(
        NOW - timedelta(hours=1), NOW + timedelta(hours=1), cursor=cut.cursor
    )
    assert [item.name for item in second.events] == ["Second"]


def test_span_context_index_tracks_open_and_crossing_spans(tmp_path):
    store = ActivityStore(tmp_path / "activity")
    started = event(
        timestamp=NOW - timedelta(hours=2),
        kind="agent.turn",
        phase="start",
        span_id="long-turn",
        status="running",
    )
    store.append(started, strict=True)

    opened = store.read_span_context(NOW - timedelta(hours=1), NOW + timedelta(hours=1))

    assert [item.event_id for item in opened.events] == [started["event_id"]]
    assert opened.partial is False
    index = store.directory / ".span-context.json"
    assert index.stat().st_mode & 0o777 == 0o600

    store.append(
        event(
            timestamp=NOW,
            kind="agent.turn",
            phase="finish",
            span_id="long-turn",
            status="ok",
        ),
        strict=True,
    )
    crossing = store.read_span_context(
        NOW - timedelta(hours=1), NOW + timedelta(hours=1)
    )

    assert [item.event_id for item in crossing.events] == [started["event_id"]]
    assert crossing.known_spans == {("trace-1", "long-turn")}

    historical = store.read_span_context(
        NOW - timedelta(hours=1), NOW - timedelta(minutes=30)
    )

    assert [item.phase for item in historical.events] == ["start", "finish"]


def test_span_context_returns_only_future_finish_for_start_inside_range(tmp_path):
    store = ActivityStore(tmp_path / "activity")
    started = event(
        timestamp=NOW - timedelta(minutes=50),
        kind="agent.turn",
        phase="start",
        span_id="inside-turn",
        status="running",
    )
    finished = event(
        timestamp=NOW + timedelta(minutes=10),
        kind="agent.turn",
        phase="finish",
        span_id="inside-turn",
        status="ok",
    )
    store.append(started, strict=True)
    store.append(finished, strict=True)

    context = store.read_span_context(
        NOW - timedelta(hours=1), NOW - timedelta(minutes=20)
    )

    assert [item.event_id for item in context.events] == [finished["event_id"]]


def test_span_context_index_repairs_a_replaced_partition(tmp_path):
    store = ActivityStore(tmp_path / "activity")
    first = event(
        timestamp=NOW - timedelta(hours=2),
        kind="agent.turn",
        phase="start",
        span_id="first",
        status="running",
    )
    store.append(first, strict=True)
    assert store.read_span_context(NOW - timedelta(hours=1), NOW).events

    replacement = event(
        timestamp=NOW - timedelta(hours=2),
        kind="agent.turn",
        phase="start",
        span_id="replacement",
        status="running",
    )
    partition = store.directory / "2026-08-15.jsonl"
    temporary = store.directory / "replacement.jsonl"
    temporary.write_text(json.dumps(replacement) + "\n")
    os.replace(temporary, partition)

    repaired = store.read_span_context(NOW - timedelta(hours=1), NOW)

    assert [item.event_id for item in repaired.events] == [replacement["event_id"]]
    assert repaired.anomalies == 1


def test_span_context_index_rebuilds_an_old_schema_without_an_anomaly(tmp_path):
    store = ActivityStore(tmp_path / "activity")
    started = event(
        timestamp=NOW - timedelta(hours=2),
        kind="agent.turn",
        phase="start",
        span_id="old-index",
        status="running",
    )
    store.append(started, strict=True)
    index = store.directory / ".span-context.json"
    index.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {},
                "open": {},
                "closed": {},
                "partial": False,
            }
        )
        + "\n"
    )

    rebuilt = store.read_span_context(NOW - timedelta(hours=1), NOW)

    assert [item.event_id for item in rebuilt.events] == [started["event_id"]]
    assert rebuilt.anomalies == 0


def test_reader_counts_malformed_unknown_version_and_duplicate_lines(tmp_path):
    store = ActivityStore(tmp_path / "activity")
    store.directory.mkdir(parents=True)
    path = store.directory / "2026-08-15.jsonl"
    valid = event()
    future = {**event(), "schema_version": 2}
    path.write_text(
        json.dumps(valid)
        + "\n"
        + json.dumps(valid)
        + "\n"
        + json.dumps(future)
        + "\nnot-json\n"
    )
    result = store.read_range(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert len(result.events) == 1
    assert result.anomalies == 3


def test_finish_without_start_duplicate_finish_and_clock_skew_are_retained(tmp_path):
    store = ActivityStore(tmp_path / "activity")
    values = [
        event(phase="finish", span_id="missing", status="ok", name="Finish only"),
        event(phase="finish", span_id="missing", status="error", name="Finish twice"),
        event(
            timestamp=NOW - timedelta(minutes=5),
            phase="start",
            span_id="skew",
            status="running",
            name="Late clock",
        ),
    ]
    for value in values:
        store.append(value, strict=True)
    result = store.read_range(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert len(result.events) == 3


def test_retention_removes_only_expired_partitions_and_never_today(tmp_path):
    store = ActivityStore(tmp_path / "activity", retention_days=90)
    store.directory.mkdir(parents=True)
    expired = store.directory / "2026-05-16.jsonl"
    boundary = store.directory / "2026-05-17.jsonl"
    today = store.directory / "2026-08-15.jsonl"
    unrelated = store.directory / "keep.txt"
    for path in (expired, boundary, today, unrelated):
        path.write_text("x")
    removed = store.cleanup_retention(now=NOW)
    assert removed == [expired]
    assert boundary.exists() and today.exists() and unrelated.exists()


def test_production_writer_invokes_retention(tmp_path):
    directory = tmp_path / "activity"
    directory.mkdir()
    today = datetime.now(timezone.utc)
    expired = directory / f"{(today - timedelta(days=91)).date().isoformat()}.jsonl"
    expired.write_text("old\n")
    expired_index = directory / ".event-ids" / expired.stem
    expired_index.mkdir(parents=True)
    (expired_index / ".offset").write_text("0\n")

    result = append_record(event(timestamp=today), directory=directory, strict=True)

    assert result.ok
    assert not expired.exists()
    assert not expired_index.exists()
    assert (
        directory / ".retention-day"
    ).read_text().strip() == today.date().isoformat()


def test_daily_cap_records_bounded_capture_health(tmp_path, monkeypatch):
    directory = tmp_path / "activity"
    monkeypatch.setattr(writer, "MAX_DAILY_BYTES", 1)

    result = append_record(
        event(timestamp=datetime.now(timezone.utc)), directory=directory
    )

    assert result.ok is False
    assert result.error == "daily activity partition is full"
    assert read_capture_health(directory)["dropped"] == 1
    assert read_capture_health(directory)["last_error"] == "daily-cap"
    assert (directory / ".capture-health.json").stat().st_mode & 0o777 == 0o600


def test_failed_disk_write_is_fail_open_or_strict(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.write_text("file")
    store = ActivityStore(occupied)
    assert store.append(event()).ok is False
    with pytest.raises(ActivityStoreError):
        store.append(event(), strict=True)


def test_range_limit_cursor_and_invalid_inputs(tmp_path):
    store = ActivityStore(tmp_path / "activity")
    for index in range(3):
        store.append(event(name=f"E{index}"), strict=True)
    first = store.read_range(
        NOW - timedelta(hours=1), NOW + timedelta(hours=1), limit=2
    )
    assert len(first.events) == 2
    assert first.partial is True
    second = store.read_range(
        NOW - timedelta(hours=1), NOW + timedelta(hours=1), cursor=first.cursor, limit=2
    )
    assert len(second.events) == 1
    with pytest.raises(ActivityStoreError, match="invalid activity cursor"):
        store.read_range(NOW, NOW, cursor="bogus")
    with pytest.raises(ActivityStoreError, match="until"):
        store.read_range(NOW, NOW - timedelta(seconds=1))


def test_partition_permissions_are_repaired_on_append(tmp_path):
    store = ActivityStore(tmp_path / "activity")
    store.directory.mkdir(mode=0o755)
    path = store.directory / "2026-08-15.jsonl"
    path.write_text("")
    path.chmod(0o644)
    index_parent = store.directory / ".event-ids"
    index_root = index_parent / "2026-08-15"
    index_root.mkdir(parents=True)
    index_parent.chmod(0o755)
    index_root.chmod(0o755)
    store.append(event(), strict=True)
    assert (store.directory.stat().st_mode & 0o777) == 0o700
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (index_parent.stat().st_mode & 0o777) == 0o700
    assert (index_root.stat().st_mode & 0o777) == 0o700
