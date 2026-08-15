"""Collision-safe reconciliation of Timeline's separate provider hook groups."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from lib.hook_files import (
    provider_hook_lock,
    read_provider_object,
    write_provider_object,
)

from .consent import capture_mode


HOOK_MARKER = "merlin:activity-timeline"
HOOK_VERSION = 1

CLAUDE_EVENTS: dict[str, str | None] = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "Stop": None,
    "PreToolUse": None,
    "PostToolUse": None,
    "PostToolUseFailure": None,
    "PermissionRequest": None,
    "Notification": "permission_prompt",
}

CODEX_EVENTS: dict[str, str | None] = {
    "SessionStart": "startup|resume|clear",
    "UserPromptSubmit": None,
    "Stop": None,
    "PreToolUse": None,
    "PostToolUse": None,
    "PermissionRequest": None,
}


def claude_settings_path() -> Path:
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    return (
        Path(custom).expanduser() / "settings.json"
        if custom
        else Path.home() / ".claude" / "settings.json"
    )


def codex_hooks_path() -> Path:
    return (
        Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
        / "hooks.json"
    )


def _command(provider: str) -> str:
    script = Path(__file__).parent / "hooks" / "activity_hook.py"
    return f'"{sys.executable}" "{script}" {provider}  # {HOOK_MARKER}:v{HOOK_VERSION}'


def _is_owned(group: object) -> bool:
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return False
    return any(
        isinstance(entry, dict) and HOOK_MARKER in str(entry.get("command", ""))
        for entry in group["hooks"]
    )


def _group(provider: str, matcher: str | None) -> dict:
    group: dict = {"hooks": [{"type": "command", "command": _command(provider)}]}
    return {"matcher": matcher, **group} if matcher is not None else group


def _read(path: Path) -> dict | None:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _shape_ok(settings: dict, events: dict[str, str | None]) -> bool:
    hooks = settings.get("hooks")
    if hooks is None:
        return True
    if not isinstance(hooks, dict):
        return False
    return all(
        hooks.get(event) is None or isinstance(hooks[event], list) for event in events
    )


def _installed(settings: dict, provider: str, events: dict[str, str | None]) -> dict:
    output = copy.deepcopy(settings)
    hooks = output.setdefault("hooks", {})
    for event, matcher in events.items():
        groups = [group for group in (hooks.get(event) or []) if not _is_owned(group)]
        groups.append(_group(provider, matcher))
        hooks[event] = groups
    return output


def _removed(settings: dict) -> dict:
    output = copy.deepcopy(settings)
    hooks = output.get("hooks")
    if not isinstance(hooks, dict):
        return output
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = [group for group in groups if not _is_owned(group)]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if not hooks:
        output.pop("hooks", None)
    return output


def _write(path: Path, value: dict, *, expected: bytes | None) -> bool:
    return write_provider_object(
        path,
        value,
        expected=expected,
        default_mode=0o600,
        prefix=".hooks-",
    )


def _reconcile_file(
    path: Path, provider: str, events: dict[str, str | None], install: bool
) -> bool:
    with provider_hook_lock(path):
        settings, original = read_provider_object(path)
        if settings is None:
            return False
        if not _shape_ok(settings, events):
            return False
        desired = (
            _installed(settings, provider, events) if install else _removed(settings)
        )
        if desired == settings:
            return False
        return _write(path, desired, expected=original)


def install_hooks() -> bool:
    try:
        claude = _reconcile_file(claude_settings_path(), "claude", CLAUDE_EVENTS, True)
    except OSError:
        claude = False
    try:
        codex = _reconcile_file(codex_hooks_path(), "codex", CODEX_EVENTS, True)
    except OSError:
        codex = False
    return claude or codex


def remove_hooks() -> bool:
    try:
        claude = _reconcile_file(claude_settings_path(), "claude", CLAUDE_EVENTS, False)
    except OSError:
        claude = False
    try:
        codex = _reconcile_file(codex_hooks_path(), "codex", CODEX_EVENTS, False)
    except OSError:
        codex = False
    return claude or codex


def hooks_drift() -> bool:
    for path, provider, events in (
        (claude_settings_path(), "claude", CLAUDE_EVENTS),
        (codex_hooks_path(), "codex", CODEX_EVENTS),
    ):
        settings = _read(path)
        if (
            settings is not None
            and _shape_ok(settings, events)
            and _installed(settings, provider, events) != settings
        ):
            return True
    return False


def sync_hooks() -> str:
    """Apply consent without allowing one provider failure to block the other."""
    try:
        mode = capture_mode()
        if mode == "off":
            return "removed" if remove_hooks() else "clean"
        if mode == "auto":
            return "synced" if install_hooks() else "in-sync"
        return "pending" if hooks_drift() else "in-sync"
    except Exception:
        return "error"
