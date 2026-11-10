"""Fail-open provider-hook runtime with stable tmux identity lookup."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
from pathlib import Path

from .consent import capture_mode
from .providers import normalize_payload
from .writer import append_record, default_activity_dir, record_capture_failure


_TMUX_FORMAT = "\x1f".join(
    (
        "#{session_name}",
        "#{window_id}",
        "#{pane_id}",
        "#{@agent_sid}",
        "#{@agent_cwd}",
        "#{pane_current_path}",
        "#{window_name}",
        "#{@agent_role}",
        "#{@agent_provider}",
        "#{@agent_model}",
        "#{@agent_effort}",
        "#{pane_pid}",
    )
)

_AGENTS = frozenset({"claude", "codex"})
# A process named after an agent but running one of these is a wrapper
# script, not an agent. The list of `_agent_nested` in
# terminal/hooks/agent-window.sh: keep both in sync.
_INTERPRETERS = frozenset(
    "bash sh dash zsh ksh mksh fish node bun deno perl ruby".split()
)


def _tmux(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=0.3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _process(pid: int) -> tuple[int, str] | None:
    """The parent and name of process `pid`, from /proc or else ps."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        if Path("/proc/self").exists():
            return None
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=0.5,
                check=False,
            )
            ppid, _, comm = result.stdout.strip().partition(" ")
            return int(ppid), comm.strip().rsplit("/", 1)[-1]
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    head, _, tail = stat.rpartition(")")
    try:
        return int(tail.split()[1]), head.partition("(")[2]
    except (IndexError, ValueError):
        return None


def _is_agent(pid: int, name: str) -> bool:
    if name not in _AGENTS:
        return False
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        # Without /proc (macOS), ps names a script by its interpreter, so a
        # process named after an agent there is the binary.
        return True
    exe = exe.removesuffix(" (deleted)")  # an interpreter upgraded under a script
    base = exe.rsplit("/", 1)[-1]
    return not (base in _INTERPRETERS or base.startswith("python"))


def nested_agent(pane_pid: int) -> bool:
    """True when another agent started the one this hook runs for.

    The same rule as `_agent_nested` in terminal/hooks/agent-window.sh: two
    agent processes from this hook up to the pane's process (included). A
    hook that does not descend from the pane gives no verdict.
    """
    pid, agents = os.getpid(), 0
    while pid > 1:
        entry = _process(pid)
        if entry is None:
            return False
        ppid, name = entry
        if _is_agent(pid, name):
            agents += 1
        if pid == pane_pid:
            return agents >= 2
        pid = ppid
    return False


def tmux_metadata(payload: dict) -> dict | None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    result = _tmux("display-message", "-p", "-t", pane, _TMUX_FORMAT)
    if result is None or result.returncode != 0:
        return None
    fields = result.stdout.rstrip("\n").split("\x1f")
    if len(fields) != 12:
        return None
    (
        session,
        window,
        actual_pane,
        agent_sid,
        pinned_cwd,
        pane_cwd,
        window_name,
        role,
        provider,
        model,
        effort,
        pane_pid,
    ) = fields
    if pane_pid.isdigit() and nested_agent(int(pane_pid)):
        return None
    session_id = payload.get("session_id")
    fallback_seed = session_id if isinstance(session_id, str) else actual_pane or pane
    fallback_digest = hashlib.sha256(
        f"{session}\0{window}\0{fallback_seed}".encode()
    ).hexdigest()[:24]
    if not pinned_cwd:
        candidate = (
            payload.get("cwd") if isinstance(payload.get("cwd"), str) else pane_cwd
        )
        pinned_cwd = candidate or pane_cwd
    project = Path(pinned_cwd).name if pinned_cwd else "unknown"
    return {
        "tmux_session": session,
        "tmux_window": window,
        "tmux_pane": actual_pane or pane,
        "agent_sid": agent_sid or None,
        "timeline_actor_id": f"timeline:{fallback_digest}",
        "cwd": pinned_cwd or pane_cwd,
        "project": project,
        "window_name": window_name,
        "role": role or os.environ.get("MERLIN_TIMELINE_ROLE") or None,
        "provider": provider or os.environ.get("MERLIN_PROVIDER") or None,
        "model": model or os.environ.get("MERLIN_MODEL") or None,
        "effort": effort or os.environ.get("MERLIN_EFFORT") or None,
    }


def process_payload(
    provider: str,
    payload: dict,
    *,
    directory: Path | None = None,
) -> int:
    """Append all normalized records and always return the provider's success code."""
    try:
        if capture_mode() != "auto":
            return 0
        metadata = tmux_metadata(payload)
        if metadata is None:
            return 0
        target = directory or default_activity_dir()
        try:
            records = normalize_payload(
                provider,
                payload,
                metadata,
                correlation_dir=target,
            )
        except Exception as exc:
            record_capture_failure(target, exc)
            return 0
        for record in records:
            append_record(record, directory=target, strict=False)
    except Exception:
        return 0
    return 0


def read_stdin_payload(*, limit: int = 1024 * 1024) -> dict | None:
    try:
        raw = os.read(0, limit + 1)
        while raw and len(raw) <= limit and not raw.endswith(b"\n"):
            chunk = os.read(0, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) > limit:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
