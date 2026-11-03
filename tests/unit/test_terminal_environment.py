"""Regression tests for terminal environment isolation."""

import subprocess

from terminal.tmux import HOOKS_DIR, scrub_server_env, terminal_process_env


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


def test_terminal_process_env_withholds_secrets_and_owned_settings(
    monkeypatch, tmp_path
):
    """A tmux server hands its environment to every shell, agent and job, and
    keeps it for its whole life: no secret goes there, and no copy of a setting
    config.env holds (it would outlive any change to the file)."""
    monkeypatch.setenv("MERLIN_HOME", str(tmp_path))
    (tmp_path / "config.env").write_text(
        "MERLIN_FEATURES=app,timeline\nAGENT_STATE_HOOKS=auto\nANTHROPIC_API_KEY=k\n"
    )
    env = terminal_process_env(
        {
            "MERLIN_FEATURES": "app",
            "AGENT_STATE_HOOKS": "auto",
            "DASHBOARD_PASS": "secret",
            "MERLIN_SAAS_TOKEN": "secret",
            "MERLIN_TTS_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "k",
            "MERLIN_HOME": str(tmp_path),
            "MERLIN_LAUNCH_CWD": "/work",
        },
        term="xterm-256color",
    )
    assert not {
        "MERLIN_FEATURES",
        "AGENT_STATE_HOOKS",
        "DASHBOARD_PASS",
        "MERLIN_SAAS_TOKEN",
        "MERLIN_TTS_API_KEY",
    } & set(env)
    # A user's own key is theirs to hand their agents, and what config.env
    # does not hold is not a stale copy.
    assert env["ANTHROPIC_API_KEY"] == "k"
    assert env["MERLIN_HOME"] == str(tmp_path)
    assert env["MERLIN_LAUNCH_CWD"] == "/work"


def test_scrub_server_env_cleans_a_running_server(monkeypatch, tmp_path):
    """A server an older Merlin started holds the settings and secrets: they
    leave its global environment, and nothing else does."""
    monkeypatch.setenv("MERLIN_HOME", str(tmp_path))
    (tmp_path / "config.env").write_text("MERLIN_FEATURES=app\n")
    calls = []

    def fake_run(argv, **_):
        calls.append(argv)
        out = "MERLIN_FEATURES=app\nDASHBOARD_PASS=s\nPATH=/bin\n-TERM_OLD\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert sorted(scrub_server_env()) == ["DASHBOARD_PASS", "MERLIN_FEATURES"]
    assert ["tmux", "set-environment", "-gu", "PATH"] not in calls


def test_scrub_server_env_without_a_server_does_nothing(monkeypatch):
    def fake_run(argv, **_):
        assert argv[:2] == ["tmux", "show-environment"]
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no server")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert scrub_server_env() == []
