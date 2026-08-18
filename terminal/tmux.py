"""Shared tmux session startup policy for terminal entry points."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from board.sweep import TmuxSession

DEFAULT_SESSION_NAME = "merlin-dev"
_SESSION_ID = re.compile(r"^\$\d+$")


@dataclass(frozen=True)
class SessionIdentity:
    """A tmux session identity that remains safe across rename and restart."""

    session_id: str
    created: int


def terminal_process_env(base_env: Mapping[str, str], *, term: str) -> dict[str, str]:
    """Build a terminal environment independent of Merlin's launch shell."""
    env = dict(base_env)
    env["TERM"] = term
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env


def parse_session_identity(
    session_id: str | None, created: str | None
) -> SessionIdentity | None:
    """Parse an untrusted browser preference, returning None when malformed."""
    if not session_id or not _SESSION_ID.fullmatch(session_id) or not created:
        return None
    try:
        created_at = int(created)
    except ValueError:
        return None
    if created_at <= 0:
        return None
    return SessionIdentity(session_id, created_at)


def reconnect_argv(
    sessions: Sequence[TmuxSession], preferred: SessionIdentity | None = None
) -> list[str]:
    """Choose how a new terminal client joins the shared tmux server."""
    if preferred is not None:
        match = next(
            (
                session
                for session in sessions
                if session.session_id == preferred.session_id
                and session.created == preferred.created
            ),
            None,
        )
        if match is not None:
            return ["tmux", "attach", "-t", match.session_id]
    if sessions:
        return ["tmux", "attach"]
    return [
        "tmux",
        "new-session",
        "-A",
        "-s",
        DEFAULT_SESSION_NAME,
        "-x",
        "120",
        "-y",
        "40",
    ]
