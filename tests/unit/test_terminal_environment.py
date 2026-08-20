"""Regression tests for terminal environment isolation."""

from terminal.tmux import terminal_process_env


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
    }
