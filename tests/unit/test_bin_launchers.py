"""Tests that the repo's bin/ launchers ship correctly.

These files are placed on PATH via ~/.merlin/current/bin (the install puts
the active version's bin/ on PATH). A release that fails to ship them
executable would break `merlin` / `merlin-clip` for every install, so guard
their presence and mode here. merlin-clip's runtime behavior is covered by
test_merlin_clip.py.
"""

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
