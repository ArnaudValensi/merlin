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

import shutil
import subprocess
from dataclasses import dataclass

# Tab-separated so free-text fields (paths, window names) survive intact — tabs
# do not occur in tmux session/window names or in normal filesystem paths. Field
# order is fixed; keep it in sync with ``_FIELDS`` and ``parse_sweep``.
_FIELDS = (
    "@agent_sid",
    "@agent_state",
    "@agent_cwd",
    "@agent_parent",
    "@agent_relation",
    "session_name",
    "window_id",
    "window_active",
    "window_activity",
    "window_name",
)
_FMT = "\t".join("#{" + f + "}" for f in _FIELDS)


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
    active: bool
    activity: int
    name: str

    @property
    def is_agent(self) -> bool:
        return bool(self.state)


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
        sid, state, cwd, parent, relation, session, wid, active, activity, name = parts
        windows.append(
            Window(
                sid=sid,
                state=state.strip().lower(),
                cwd=cwd,
                parent=parent,
                relation=relation.strip().lower(),
                session=session,
                window_id=wid,
                active=active == "1",
                activity=int(activity) if activity.isdigit() else 0,
                name=name,
            )
        )
    return windows


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


def focus_window(session: str, window_id: str) -> bool:
    """Make ``window_id`` the active window in ``session`` (jump-to-window).

    Returns True on success. Best-effort: a stale id or a dead session just
    returns False.
    """
    if not shutil.which("tmux") or not session or not window_id:
        return False
    try:
        proc = subprocess.run(
            ["tmux", "select-window", "-t", f"{session}:{window_id}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0
