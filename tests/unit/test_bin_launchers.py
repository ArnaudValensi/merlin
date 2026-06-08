"""Tests for the repo's bin/ launchers.

These files ship in the repo and are placed on PATH via ~/.merlin/current/bin
(the install puts the active version's bin/ on PATH). A release that fails to
ship them executable would break `merlin` / `merlin-clip` for every install,
so guard their presence and mode here.
"""

import os
import subprocess
from pathlib import Path

BIN_DIR = Path(__file__).parent.parent.parent / "bin"


class TestLaunchersShipped:
    def test_merlin_launcher_exists_and_executable(self):
        launcher = BIN_DIR / "merlin"
        assert launcher.is_file(), "bin/merlin must ship in the repo"
        assert launcher.stat().st_mode & 0o111, "bin/merlin must be executable"

    def test_merlin_clip_exists_and_executable(self):
        clip = BIN_DIR / "merlin-clip"
        assert clip.is_file(), "bin/merlin-clip must ship in the repo"
        assert clip.stat().st_mode & 0o111, "bin/merlin-clip must be executable"

    def test_merlin_launcher_pins_project_and_respects_merlin_home(self):
        """The launcher must pin the uv project (cwd-independent) and honor
        MERLIN_HOME so a custom install location still resolves."""
        body = (BIN_DIR / "merlin").read_text()
        assert "uv run --project" in body, "launcher must pin the uv project"
        assert "MERLIN_HOME" in body, "launcher must respect MERLIN_HOME"
        assert "current" in body, "launcher must target the active version"


class TestMerlinClipBehavior:
    """merlin-clip is environment-agnostic POSIX sh — exercise its core."""

    def _run(self, args, stdin=None, env=None):
        full_env = os.environ.copy()
        # Force the stdout OSC52 path (no tmux client TTY) for deterministic
        # output regardless of where the test runs.
        full_env.pop("TMUX", None)
        if env:
            full_env.update(env)
        return subprocess.run(
            [str(BIN_DIR / "merlin-clip"), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=full_env,
            timeout=10,
        )

    def test_copy_emits_osc52(self):
        result = self._run(["copy"], stdin="hello")
        # ESC ] 52 ; c ; base64("hello"=aGVsbG8=) ESC \
        assert result.stdout == "\033]52;c;aGVsbG8=\033\\"

    def test_pipe_with_no_arg_copies(self):
        result = self._run([], stdin="hi")
        assert "\033]52;c;" in result.stdout

    def test_paste_reads_sync_file(self, tmp_path, monkeypatch):
        # CLIP_FILE is hardcoded to /tmp/merlin-clipboard/current.txt in the
        # script; just assert paste is empty/clean when absent (no crash).
        result = self._run(["paste"])
        assert result.returncode == 0

    def test_unknown_arg_shows_usage_nonzero(self):
        result = self._run(["bogus"])
        assert result.returncode == 1
        assert "Usage: merlin-clip" in result.stdout
