"""Phase 0 activity append probe; product storage is intentionally not frozen."""

import argparse
import fcntl
import json
import os
from pathlib import Path


def append_record(directory: Path, record: dict, *, strict: bool = False) -> bool:
    """Append one locked JSON line, failing open unless strict was requested."""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / "probe.jsonl"
        data = (json.dumps(record, separators=(",", ":")) + "\n").encode()
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            written = os.write(fd, data)
            if written != len(data):
                raise OSError("short append")
        finally:
            os.close(fd)
        return True
    except (OSError, TypeError, ValueError):
        if strict:
            raise
        return False


def read_records(path: Path) -> tuple[list[dict], int]:
    """Read known and future records while counting malformed lines."""
    records = []
    anomalies = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records, 1
    for line in lines:
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            anomalies += 1
            continue
        if not isinstance(value, dict):
            anomalies += 1
            continue
        records.append(value)
    return records, anomalies


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    ok = append_record(
        args.directory,
        {"event_id": args.event_id, "kind": "probe.future", "future": True},
        strict=args.strict,
    )
    return 0 if ok or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
