"""Regression tests for terminal environment isolation."""

from terminal.tmux import HOOKS_DIR, terminal_process_env


def test_terminal_process_env_replaces_launcher_terminal_metadata():
    env = terminal_process_env(
        {
            "PATH": "/usr/bin",
            "TERM": "dumb",
            "TMUX": "/tmp/tmux/default,1,0",
            "TMUX_PANE": "%3",
            "MERLIN_KEEP": "yes",
        },
        term="xterm-256color",
    )

    assert env == {
        "PATH": "/usr/bin",
        "TERM": "xterm-256color",
        "MERLIN_KEEP": "yes",
        "MERLIN_TERMINAL_HOOKS": str(HOOKS_DIR),
    }


def test_terminal_process_env_always_points_at_the_hooks_directory():
    """tmux.conf's hooks resolve their scripts through this variable.

    It is set for every entry point rather than at one call site: a tmux server
    created without it runs those hooks as no-ops for its entire lifetime, and
    which entry point happens to create the server is not something any single
    call site can know.
    """
    env = terminal_process_env({}, term="xterm-256color")

    assert env["MERLIN_TERMINAL_HOOKS"] == str(HOOKS_DIR)
    assert (HOOKS_DIR / "agent-state-switch.sh").exists()
