"""Shared tmux session startup policy for terminal entry points."""

from collections.abc import Sequence

from board.sweep import TmuxSession

DEFAULT_SESSION_NAME = "merlin-dev"


def reconnect_argv(sessions: Sequence[TmuxSession]) -> list[str]:
    """Choose how a new terminal client joins the shared tmux server."""
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
