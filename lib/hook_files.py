"""Shared collision-safe updates for user-owned provider hook files."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


LOCK_TIMEOUT_SECONDS = 0.25
LOCK_POLL_SECONDS = 0.005


class ProviderHookLockTimeout(TimeoutError):
    pass


@contextmanager
def provider_hook_lock(path: Path) -> Iterator[None]:
    """Serialize Merlin's provider-file writers within a bounded budget."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".merlin-lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ProviderHookLockTimeout(
                        "provider hook lock timed out"
                    ) from exc
                time.sleep(
                    min(
                        LOCK_POLL_SECONDS,
                        max(0.0, deadline - time.monotonic()),
                    )
                )
        yield
    finally:
        os.close(lock_fd)


def read_provider_object(path: Path) -> tuple[dict | None, bytes | None]:
    """Read an object plus its exact bytes; missing files are empty objects."""
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    try:
        value = json.loads(original)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, original
    return (value if isinstance(value, dict) else None), original


def write_provider_object(
    path: Path,
    value: dict,
    *,
    expected: bytes | None,
    default_mode: int,
    prefix: str,
) -> bool:
    """Atomically replace unchanged provider JSON and preserve its mode."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rendered = json.dumps(value, indent=2) + "\n"
    json.loads(rendered)
    mode = path.stat().st_mode & 0o777 if path.exists() else default_mode
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(rendered)
        os.chmod(temporary, mode)
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != expected:
            os.unlink(temporary)
            return False
        os.replace(temporary, path)
        return True
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
