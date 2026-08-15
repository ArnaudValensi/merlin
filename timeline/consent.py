"""Independent consent and config persistence for activity history capture."""

from __future__ import annotations

import os
import tempfile

import paths


CAPTURE_KEY = "MERLIN_ACTIVITY_HOOKS"
CAPTURE_MODES = ("auto", "ask", "off")
CAPTURE_DEFAULT = "ask"


def capture_setting() -> tuple[str, str]:
    """Return mode and source; saved consent wins over stale inherited env."""
    try:
        lines = paths.config_path().read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        candidate = line.strip().removeprefix("export ")
        key, separator, configured = candidate.partition("=")
        if separator and key.strip() == CAPTURE_KEY:
            normalized = configured.strip().strip("\"'").lower()
            return (
                normalized if normalized in CAPTURE_MODES else CAPTURE_DEFAULT,
                "config",
            )
    normalized = (os.environ.get(CAPTURE_KEY) or CAPTURE_DEFAULT).strip().lower()
    return (
        normalized if normalized in CAPTURE_MODES else CAPTURE_DEFAULT,
        "environment" if CAPTURE_KEY in os.environ else "default",
    )


def capture_mode() -> str:
    return capture_setting()[0]


def set_capture_mode(mode: str) -> str:
    normalized = (mode or "").strip().lower()
    if normalized not in CAPTURE_MODES:
        raise ValueError(f"invalid activity capture mode {mode!r}")
    path = paths.config_path()
    try:
        original = path.read_text().splitlines()
    except OSError:
        original = []
    replacement = f"{CAPTURE_KEY}={normalized}"
    output: list[str] = []
    replaced = False
    for line in original:
        candidate = line.strip().removeprefix("export ")
        key, separator, _value = candidate.partition("=")
        if separator and key.strip() == CAPTURE_KEY:
            if not replaced:
                output.append(replacement)
                replaced = True
            continue
        output.append(line)
    if not replaced:
        output.append(replacement)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    os.environ[CAPTURE_KEY] = normalized
    return normalized
