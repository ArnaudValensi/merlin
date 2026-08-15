"""Evidence for the provider-neutral append mechanics chosen in Phase 0."""

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from tests.spikes.activity_writer_probe import append_record, read_records


def _append_worker(args: tuple[str, int]) -> bool:
    directory, event_id = args
    return append_record(
        Path(directory),
        {
            "schema_version": 1,
            "event_id": f"event-{event_id}",
            "kind": "probe.concurrent",
            "unknown_future_field": event_id,
        },
        strict=True,
    )


def test_many_processes_append_complete_unique_lines(tmp_path):
    directory = tmp_path / "activity"
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=8) as pool:
        accepted = pool.map(_append_worker, [(str(directory), i) for i in range(160)])

    lines = (directory / "probe.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert all(accepted)
    assert len(records) == 160
    assert len({record["event_id"] for record in records}) == 160
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert ((directory / "probe.jsonl").stat().st_mode & 0o777) == 0o600


def test_reader_skips_malformed_tail_and_keeps_unknown_data(tmp_path):
    path = tmp_path / "probe.jsonl"
    path.write_text(
        '{"event_id":"known","kind":"probe"}\n'
        '{"event_id":"future","kind":"future.kind","extra":{"v":2}}\n'
        '{"event_id":"cut"'
    )
    records, anomalies = read_records(path)
    assert [record["event_id"] for record in records] == ["known", "future"]
    assert records[1]["extra"] == {"v": 2}
    assert anomalies == 1


def test_fail_open_does_not_change_caller_status(tmp_path):
    unwritable_target = tmp_path / "not-a-directory"
    unwritable_target.write_text("occupied")
    assert append_record(unwritable_target, {"event_id": "ignored"}) is False
    with pytest.raises(OSError):
        append_record(unwritable_target, {"event_id": "strict"}, strict=True)


def test_probe_has_no_server_dependency(tmp_path, monkeypatch):
    monkeypatch.setenv("MERLIN_SERVER", "http://127.0.0.1:1")
    assert append_record(tmp_path / "activity", {"event_id": "offline"})
    assert os.environ["MERLIN_SERVER"].endswith(":1")
