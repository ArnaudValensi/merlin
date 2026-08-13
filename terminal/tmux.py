"""Shared tmux session startup policy for terminal entry points."""

from collections.abc import Mapping, Sequence

from board.sweep import TmuxSession

DEFAULT_SESSION_NAME = "merlin-dev"


def terminal_process_env(base_env: Mapping[str, str], *, term: str) -> dict[str, str]:
    """Build a terminal environment independent of Merlin's launch shell."""
    env = dict(base_env)
    env["TERM"] = term
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env


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
