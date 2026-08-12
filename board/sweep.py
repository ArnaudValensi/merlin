"""tmux sweep: read every window's agent-state signal in one call.

The Sessions board is a 2D view over the same per-window `@agent_state` the
status-bar pills already stamp (see ``terminal/hooks/agent-state.sh``). This
module runs a single ``tmux list-windows -a -F`` to pull every window plus the
board's window options, and parses the output into ``Window`` records.

No tmux state is written here. Durable identity (`@agent_sid`) and the pinned
launch cwd (`@agent_cwd`) are stamped by the SessionStart hook
(``terminal/hooks/agent-session-init.sh``); family links (`@agent_parent`,
`@agent_relation`) by whatever spawned the window (fork/handoff). We only read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

# Tab-separated so free-text fields (paths, window names) survive intact — tabs
# do not occur in tmux session/window names or in normal filesystem paths. Field
# order is fixed; keep it in sync with ``_FIELDS`` and ``parse_sweep``.
_FIELDS = (
    "@agent_sid",
    "@agent_state",
    "@claude",
    "@agent_cwd",
    "@agent_parent",
    "@agent_relation",
    "session_name",
    "window_id",
    "window_index",
    "window_active",
    "window_activity",
    "window_name",
)
_FMT = "\t".join("#{" + f + "}" for f in _FIELDS)

# Session-tier sweep: one row per tmux session, for the session switcher.
_SESSION_FIELDS = (
    "session_name",
    "session_id",
    "session_attached",
    "session_windows",
    "session_activity",
)
_SESSION_FMT = "\t".join("#{" + f + "}" for f in _SESSION_FIELDS)


@dataclass(frozen=True)
class Window:
    """One tmux window as the board sees it.

    ``sid``/``cwd``/``parent``/``relation`` are empty strings when the window
    option is unset (a plain shell window, or an agent window that started before
    the board hook was installed). ``is_agent`` gates the rich vs plain tier:
    a window is an agent card iff it carries an ``@agent_state``.
    """

    sid: str
    state: str
    cwd: str
    parent: str
    relation: str
    session: str
    window_id: str
    index: int
    active: bool
    activity: int
    name: str

    @property
    def is_agent(self) -> bool:
        return bool(self.state)


@dataclass(frozen=True)
class TmuxSession:
    """One tmux session as the switcher sees it. ``attached`` is True when at
    least one client is on it; ``windows`` is its window count."""

    name: str
    session_id: str
    attached: bool
    windows: int
    activity: int


def parse_sweep(raw: str) -> list[Window]:
    """Parse ``tmux list-windows -a -F`` output (pure, no I/O).

    Lines with the wrong field count are skipped rather than raising, so a tmux
    quirk can never take the board down.
    """
    windows: list[Window] = []
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != len(_FIELDS):
            continue
        (
            sid,
            state,
            claude,
            cwd,
            parent,
            relation,
            session,
            wid,
            index,
            active,
            activity,
            name,
        ) = parts
        # Coalesce the state variable. Vanilla Merlin drives `@agent_state`; a
        # custom pill system (e.g. the maintainer's dotfiles) may drive `@claude`
        # instead and be the one actually cleared on visit. Prefer `@claude` when
        # set so the board tracks whichever variable is live, and never shows a
        # stale "done" that the tmux status bar has already cleared.
        effective = (claude.strip() or state.strip()).lower()
        windows.append(
            Window(
                sid=sid,
                state=effective,
                cwd=cwd,
                parent=parent,
                relation=relation.strip().lower(),
                session=session,
                window_id=wid,
                index=int(index) if index.lstrip("-").isdigit() else 0,
                active=active == "1",
                activity=int(activity) if activity.isdigit() else 0,
                name=name,
            )
        )
    return windows


def parse_sessions(raw: str) -> list[TmuxSession]:
    """Parse ``tmux list-sessions -F`` output (pure, no I/O). Malformed rows are
    skipped so a tmux quirk can never take the switcher down."""
    out: list[TmuxSession] = []
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != len(_SESSION_FIELDS):
            continue
        name, sid, attached, windows, activity = parts
        out.append(
            TmuxSession(
                name=name,
                session_id=sid,
                attached=attached.isdigit() and int(attached) > 0,
                windows=int(windows) if windows.isdigit() else 0,
                activity=int(activity) if activity.isdigit() else 0,
            )
        )
    return out


def run_sweep() -> list[Window]:
    """Run the tmux sweep and parse it. Returns [] if tmux is unavailable or
    errors — the board simply shows nothing rather than failing the request."""
    if not shutil.which("tmux"):
        return []
    try:
        proc = subprocess.run(
            ["tmux", "list-windows", "-a", "-F", _FMT],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return parse_sweep(proc.stdout)


def run_session_sweep() -> list[TmuxSession]:
    """Run the tmux session sweep and parse it. Returns [] if tmux is
    unavailable or errors, so the switcher shows nothing rather than failing."""
    out = _tmux_capture(["list-sessions", "-F", _SESSION_FMT])
    return parse_sessions(out) if out is not None else []


def _tmux_capture(args: list[str]) -> str | None:
    """Run a read-only tmux command, returning stdout or None on any failure."""
    if not shutil.which("tmux"):
        return None
    try:
        proc = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def client_session(tty: str) -> str | None:
    """The tmux session a client (identified by its tty) is attached to.

    This is how the web terminal learns which session *this* browser is on, so
    switching stays per-client (one browser tab switching never moves another).
    Returns None if tmux is unavailable or the client is unknown.
    """
    if not tty:
        return None
    out = _tmux_capture(["display-message", "-p", "-t", tty, "#{client_session}"])
    return out.strip() if out else None


def sanitize_session_name(name: str) -> str:
    """tmux forbids ``.`` and ``:`` in session names (and empty names). Map them
    to underscores and trim, so a project directory basename is always usable."""
    cleaned = name.strip().replace(".", "_").replace(":", "_").strip()
    return cleaned or "session"


def focus_window(session: str, window_id: str) -> bool:
    """Make ``window_id`` the active window in ``session`` (jump-to-window).

    Returns True on success. Best-effort: a stale id or a dead session just
    returns False.
    """
    return _window_cmd("select-window", session, window_id)


def switch_client(tty: str, target: str) -> bool:
    """Point one client (by tty) at ``target`` (``session`` or ``session:window``).

    Per-client: uses ``switch-client -c <tty>`` so only this browser's tmux
    client moves. ``session:window`` both switches session and selects the
    window in one call, which is how cross-session jump-to-window works.
    """
    if not shutil.which("tmux") or not tty or not target:
        return False
    return _run_ok(["switch-client", "-c", tty, "-t", target])


def rename_session(old: str, new: str) -> bool:
    """Rename a tmux session. Best-effort; returns False on any failure."""
    if not old or not new:
        return False
    return _run_ok(["rename-session", "-t", old, sanitize_session_name(new)])


def rename_window(session: str, window_id: str, new: str) -> bool:
    """Rename a tmux window (the tmux window_name shown as its tab title)."""
    if not session or not window_id or not new:
        return False
    return _run_ok(["rename-window", "-t", f"{session}:{window_id}", new])


def kill_session(name: str) -> bool:
    """Kill an entire tmux session. Best-effort; returns False on any failure."""
    if not name:
        return False
    return _run_ok(["kill-session", "-t", name])


def new_window(session: str) -> str | None:
    """Create a new window in ``session`` and return its window id, so the
    caller can jump to it. Returns None if tmux is unavailable or it fails."""
    if not shutil.which("tmux") or not session:
        return None
    out = _tmux_capture(["new-window", "-t", session, "-P", "-F", "#{window_id}"])
    return out.strip() if out else None


def create_or_get_session(directory: str, name: str = "") -> str | None:
    """Create-or-switch by directory: return the name of a detached session
    rooted at ``directory``, creating it if one does not already exist.

    The session name is ``name`` if given, else the directory basename,
    sanitised for tmux. If a session with that name already exists, it is
    reused (create-or-switch), so re-opening a project just returns you to it.
    Returns None if tmux is unavailable or creation fails.
    """
    if not shutil.which("tmux") or not directory:
        return None
    base = name.strip() or os.path.basename(directory.rstrip("/"))
    session = sanitize_session_name(base)
    existing = {s.name for s in run_session_sweep()}
    if session in existing:
        return session
    # Detached create (-d): the browser attaches by switch-client afterwards,
    # keeping session creation global but switching per-client.
    if _run_ok(["new-session", "-d", "-s", session, "-c", directory]):
        return session
    return None


def kill_window(session: str, window_id: str) -> bool:
    """Close a session by killing its tmux window (the user's explicit close).

    Returns True on success. Best-effort: a stale id or a dead session just
    returns False.
    """
    return _window_cmd("kill-window", session, window_id)


def _window_cmd(cmd: str, session: str, window_id: str) -> bool:
    if not shutil.which("tmux") or not session or not window_id:
        return False
    return _run_ok([cmd, "-t", f"{session}:{window_id}"])


def _run_ok(args: list[str]) -> bool:
    """Run a mutating tmux command, returning True on exit 0. Best-effort: a
    stale target or a dead tmux just yields False rather than raising."""
    if not shutil.which("tmux"):
        return False
    try:
        proc = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0
