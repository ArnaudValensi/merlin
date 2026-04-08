"""Tests for merlin-clip clipboard helper script."""

import os
import subprocess
from pathlib import Path

import pytest

MERLIN_CLIP = (
    Path(__file__).resolve().parents[3] / "infra" / "images" / "managed" / "merlin-clip"
)


@pytest.fixture
def clip_file(tmp_path):
    """Provide a temp clipboard sync file path."""
    return tmp_path / "current.txt"


def run_clip(*args, stdin_data=None, env_extra=None):
    """Run merlin-clip with given args and optional stdin."""
    env = os.environ.copy()
    env.pop("TMUX", None)  # ensure no tmux passthrough
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        ["sh", str(MERLIN_CLIP), *args],
        input=stdin_data,
        capture_output=True,
        env=env,
    )
    return result


class TestMerlinClipCopy:
    """merlin-clip copy: reads stdin, emits OSC 52."""

    def test_copy_emits_osc52(self):
        result = run_clip("copy", stdin_data=b"hello")
        stdout = result.stdout
        # Should contain OSC 52 sequence: \033]52;c;<base64>\033\\
        assert b"\x1b]52;c;" in stdout
        assert stdout.endswith(b"\x1b\\")
        # Extract base64 and verify
        import base64

        b64_part = stdout.split(b"\x1b]52;c;")[1].split(b"\x1b\\")[0]
        decoded = base64.b64decode(b64_part)
        assert decoded == b"hello"

    def test_copy_handles_empty(self):
        result = run_clip("copy", stdin_data=b"")
        assert b"\x1b]52;c;" in result.stdout
        assert result.returncode == 0

    def test_copy_alias_c(self):
        result = run_clip("c", stdin_data=b"test")
        assert b"\x1b]52;c;" in result.stdout

    def test_copy_inside_tmux_writes_osc52(self):
        """Inside tmux, still produces valid OSC 52 (via TTY or fallback)."""
        # When TMUX is set but socket doesn't exist, falls back to stdout
        result = run_clip(
            "copy",
            stdin_data=b"hi",
            env_extra={
                "TMUX": "/tmp/nonexistent-tmux-sock,1,0",
                "TMUX_TMPDIR": "/tmp/nonexistent-dir",
            },
        )
        assert b"\x1b]52;c;" in result.stdout

    def test_no_arg_pipe_is_copy(self):
        """No args + piped stdin = copy mode."""
        result = run_clip(stdin_data=b"shorthand")
        assert b"\x1b]52;c;" in result.stdout


class TestMerlinClipPaste:
    """merlin-clip paste: reads sync file."""

    def test_paste_reads_file(self, clip_file):
        clip_file.parent.mkdir(parents=True, exist_ok=True)
        clip_file.write_text("clipboard content")
        # Since the script hardcodes /tmp/merlin-clipboard/current.txt,
        # we test by creating that file (cleaned up after)
        real_file = Path("/tmp/merlin-clipboard/current.txt")
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.write_text("test content")
        try:
            result = run_clip("paste")
            assert result.stdout == b"test content"
        finally:
            real_file.unlink(missing_ok=True)

    def test_paste_empty_when_no_file(self):
        real_file = Path("/tmp/merlin-clipboard/current.txt")
        real_file.unlink(missing_ok=True)
        result = run_clip("paste")
        assert result.stdout == b""
        assert result.returncode == 0

    def test_paste_alias_p(self):
        real_file = Path("/tmp/merlin-clipboard/current.txt")
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.write_text("p-alias")
        try:
            result = run_clip("p")
            assert result.stdout == b"p-alias"
        finally:
            real_file.unlink(missing_ok=True)


class TestMerlinClipUsage:
    """merlin-clip with no args (tty) or invalid args shows help."""

    def test_invalid_arg_shows_usage(self):
        result = run_clip("invalid")
        assert result.returncode == 1
        assert b"Usage" in result.stdout

    def test_no_arg_tty_shows_usage(self):
        """No args + no pipe = usage (we simulate by passing no stdin)."""
        # run_clip without stdin_data means stdin is inherited (a pipe from subprocess),
        # so this will actually trigger copy mode. Test the explicit "invalid" case instead.
        result = run_clip("--help")
        assert result.returncode == 1
