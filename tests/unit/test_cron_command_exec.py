"""Tests for cron command-job execution (_run_command).

These use real subprocess with trivial commands — they never invoke the agent.
"""

import pytest

pytest.importorskip("croniter")

from cron.runner import _run_command


def test_echo_succeeds():
    """A simple echo runs with exit 0 and captures stdout."""
    result = _run_command("t", {"type": "command", "command": "echo hi"})
    assert result.exit_code == 0
    assert "hi" in result.result
    assert result.cost_usd is None
    assert result.session_id is None
    assert result.duration >= 0


def test_failing_command_exit_code():
    """A non-zero exit is reported with no cost / session."""
    result = _run_command("t", {"type": "command", "command": "exit 3"})
    assert result.exit_code == 3
    assert result.cost_usd is None
    assert result.session_id is None


def test_stderr_captured_in_combined_output():
    """stderr is merged into the combined result output."""
    result = _run_command("t", {"type": "command", "command": "echo oops >&2; exit 1"})
    assert result.exit_code == 1
    assert "oops" in result.result
    assert "oops" in result.stderr


def test_respects_working_dir(tmp_path):
    """The command runs in the configured working_dir."""
    result = _run_command(
        "t",
        {"type": "command", "command": "pwd", "working_dir": str(tmp_path)},
    )
    assert result.exit_code == 0
    # macOS may symlink /tmp -> /private/tmp; compare resolved real paths.
    import os

    assert os.path.realpath(result.result.strip()) == os.path.realpath(str(tmp_path))


def test_working_dir_falls_back_to_launch_cwd(tmp_path, monkeypatch):
    """With no working_dir, MERLIN_LAUNCH_CWD is used."""
    monkeypatch.setenv("MERLIN_LAUNCH_CWD", str(tmp_path))
    result = _run_command("t", {"type": "command", "command": "pwd"})
    import os

    assert os.path.realpath(result.result.strip()) == os.path.realpath(str(tmp_path))


def test_timeout_returns_124(monkeypatch):
    """A command exceeding the timeout returns exit_code 124."""
    monkeypatch.setattr("cron.runner.COMMAND_TIMEOUT_SECONDS", 1)
    result = _run_command("t", {"type": "command", "command": "sleep 5"})
    assert result.exit_code == 124
    assert "timed out" in result.result.lower()
