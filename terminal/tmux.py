"""Shared tmux session startup policy for every entry point that runs tmux.

This module owns the *whole* policy, not just the argv: which session to join,
and the configuration the server must be born with. tmux reads its config only
when it **creates** the server, and silently ignores ``-f`` on a client that
attaches to a running one. So whichever entry point happens to start the server
decides, for that server's entire lifetime, whether it has agent-state pills and
the done-pill clearing hooks. Keeping that knowledge here is what stops a
half-configured server depending on which door the user came through.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keeps this module free of a runtime dependency on board
    from board.sweep import TmuxSession

TERMINAL_DIR = Path(__file__).parent.resolve()
TMUX_CONF = TERMINAL_DIR / "tmux.conf"
HOOKS_DIR = TERMINAL_DIR / "hooks"

DEFAULT_SESSION_NAME = "merlin-dev"
_SESSION_ID = re.compile(r"^\$\d+$")


def tmux_conf_args() -> list[str]:
    """The ``-f <conf>`` prefix every tmux invocation should carry.

    Safe to pass unconditionally: tmux honours it when creating the server and
    ignores it when attaching, so an entry point never has to know whether it is
    the one starting the server.
    """
    return ["-f", str(TMUX_CONF)] if TMUX_CONF.exists() else []


def with_tmux_conf(argv: list[str]) -> list[str]:
    """Splice the config flag into a ``["tmux", ...]`` argv, after ``tmux``."""
    return [*argv[:1], *tmux_conf_args(), *argv[1:]]


@dataclass(frozen=True)
class SessionIdentity:
    """A tmux session identity that remains safe across rename and restart."""

    session_id: str
    created: int


def terminal_process_env(base_env: Mapping[str, str], *, term: str) -> dict[str, str]:
    """Build a terminal environment independent of Merlin's launch shell.

    ``MERLIN_TERMINAL_HOOKS`` is set here rather than at one call site because
    ``tmux.conf``'s hooks resolve their scripts through it, and a server created
    without it runs those hooks as no-ops for its whole life.
    """
    env = dict(base_env)
    env["TERM"] = term
    env["MERLIN_TERMINAL_HOOKS"] = str(HOOKS_DIR)
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
    sessions: Sequence["TmuxSession"], preferred: SessionIdentity | None = None
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
