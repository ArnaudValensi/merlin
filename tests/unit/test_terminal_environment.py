"""Regression tests for terminal environment isolation."""

import os
import shutil
import subprocess
import time
from pathlib import Path

from terminal.tmux import terminal_process_env

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_restart_script_sanitizes_poisoned_launcher_environment(tmp_path):
    app_dir = tmp_path / "app"
    fake_bin = tmp_path / "bin"
    capture = tmp_path / "started-env"
    app_dir.mkdir()
    fake_bin.mkdir()
    shutil.copy2(REPO_ROOT / "restart.sh", app_dir / "restart.sh")

    def executable(name: str, contents: str) -> None:
        path = fake_bin / name
        path.write_text(contents)
        path.chmod(0o755)

    executable("pgrep", "#!/bin/bash\necho 4242\n")
    executable("pkill", "#!/bin/bash\nexit 0\n")
    executable("sleep", "#!/bin/bash\nexit 0\n")
    executable(
        "uv",
        """#!/bin/bash
{
    printf 'TERM=%s\n' "${TERM-unset}"
    printf 'TMUX=%s\n' "${TMUX-unset}"
    printf 'TMUX_PANE=%s\n' "${TMUX_PANE-unset}"
} > "$MERLIN_TEST_CAPTURE"
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "TERM": "dumb",
            "TMUX": "/tmp/tmux/default,1,0",
            "TMUX_PANE": "%3",
            "MERLIN_TEST_CAPTURE": str(capture),
        }
    )
    result = subprocess.run(
        ["bash", str(app_dir / "restart.sh")],
        cwd=app_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 2
    while not capture.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert capture.read_text().splitlines() == [
        "TERM=xterm-256color",
        "TMUX=unset",
        "TMUX_PANE=unset",
    ]
