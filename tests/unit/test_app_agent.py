"""The agent's side of apps: screenshots, input, and the merlin-app skill."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import ext_commands
from app import sessions
from app.sessions import AppError
from lib import skills
from test_app_sessions import COMMANDS, PROBE, pytestmark  # noqa: F401


@pytest.fixture
def probe(monkeypatch, tmp_path):
    log = tmp_path / "probe.log"
    monkeypatch.setenv("X_PROBE_LOG", str(log))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    sessions.launch([str(PROBE)], name="probe", gpu="off")
    yield log
    for record in sessions.list_sessions():
        sessions.stop(record["id"])


def _events(log: Path, expected: list[str], timeout: float = 10) -> list[str]:
    """Wait for every expected probe line; fail naming the missing ones."""
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    while time.monotonic() < deadline:
        lines = log.read_text().splitlines() if log.exists() else []
        if all(line in lines for line in expected):
            return lines
        time.sleep(0.05)
    missing = [line for line in expected if line not in lines]
    raise AssertionError(f"probe never logged {missing}; got {lines[-20:]}")


def _pixel(png: Path, x: int, y: int) -> str:
    return subprocess.run(
        ["magick", str(png), "-format", f"%[pixel:p{{{x},{y}}}]", "info:"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _size(png: Path) -> str:
    return subprocess.run(
        ["magick", str(png), "-format", "%wx%h", "info:"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.mark.skipif(shutil.which("magick") is None, reason="needs ImageMagick")
def test_screenshot_shows_the_app(probe, tmp_path):
    path = sessions.screenshot("probe", tmp_path / "shot.png")
    assert _size(path) == "1280x720"
    assert _pixel(path, 10, 10) == "srgb(255,0,255)"  # the probe's square
    assert _pixel(path, 900, 600) == "srgb(0,255,136)"  # filled to the display


def test_screenshot_default_path(probe):
    path = sessions.screenshot("probe")
    assert path.parent == sessions.shots_dir()
    assert path.suffix == ".png" and path.stat().st_size > 0


def test_keys_reach_the_app(probe):
    sessions.send_keys("probe", ["x", "Right"], delay_ms=20)
    lines = _events(probe, ["keydown x", "keyup x", "keydown Right", "keyup Right"])
    assert lines.index("keydown x") < lines.index("keydown Right")


def test_repeat(probe):
    sessions.send_keys("probe", ["z"], repeat=3, delay_ms=20)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if probe.exists() and probe.read_text().count("keydown z") == 3:
            break
        time.sleep(0.05)
    assert probe.read_text().count("keydown z") == 3


def test_type_click_and_move(probe):
    sessions.type_text("probe", "ab")
    sessions.click("probe", 300, 200)
    sessions.click("probe", 310, 210, button=3)
    _events(
        probe,
        [
            "keydown a",
            "keyup a",
            "keydown b",
            "keyup b",
            "btndown 1 300 200",
            "btnup 1 300 200",
            "btndown 3 310 210",
            "btnup 3 310 210",
        ],
    )


def test_agent_input_is_recorded(probe):
    assert sessions.get("probe")["last_agent_input_at"] is None
    sessions.send_keys("probe", ["x"])
    assert sessions.get("probe")["last_agent_input_at"]


def test_input_needs_a_running_app(probe):
    with pytest.raises(KeyError):
        sessions.send_keys("ghost", ["x"])
    sessions.stop("probe")
    with pytest.raises(KeyError):
        sessions.screenshot("probe")


def test_input_to_an_exited_app_fails(monkeypatch, tmp_path):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    sessions.launch(
        [str(PROBE), "--exit-after", "0.2"], name="brief", gpu="off", wait=0
    )
    try:
        deadline = time.monotonic() + 10
        while sessions.get("brief")["status"] != "exited":
            assert time.monotonic() < deadline
            time.sleep(0.1)
        with pytest.raises(AppError, match="not running"):
            sessions.send_keys("brief", ["x"])
    finally:
        sessions.stop("brief")


def test_cli_screenshot_and_input(probe, tmp_path):
    out = tmp_path / "cli.png"
    shot = subprocess.run(
        [str(COMMANDS / "screenshot.py"), "probe", "-o", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert shot.returncode == 0, shot.stderr
    assert json.loads(shot.stdout) == {"path": str(out)}
    key = subprocess.run(
        [str(COMMANDS / "input.py"), "probe", "key", "Escape"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert key.returncode == 0, key.stderr
    _events(probe, ["keydown Escape", "keyup Escape"])


class TestSkill:
    def test_aggregated_only_with_the_flag(self, monkeypatch):
        monkeypatch.setenv("MERLIN_FEATURES", "app")
        on = skills.build_registry(ext_commands.enabled_extension_source_dirs())
        assert "merlin-app" in on
        monkeypatch.setenv("MERLIN_FEATURES", "")
        off = skills.build_registry(ext_commands.enabled_extension_source_dirs())
        assert "merlin-app" not in off

    def test_frontmatter(self):
        meta = skills.parse_skill_frontmatter(
            Path(__file__).resolve().parents[2]
            / "app"
            / "skills"
            / "merlin-app"
            / "SKILL.md"
        )
        assert meta["name"] == "merlin-app"
        assert "merlin app" in meta["description"]


def test_hold_keeps_the_key_down(probe):
    start = time.monotonic()
    sessions.send_keys("probe", ["Up"], hold_ms=300, delay_ms=0)
    assert time.monotonic() - start >= 0.3
    lines = _events(probe, ["keydown Up", "keyup Up"])
    assert lines.index("keydown Up") < lines.index("keyup Up")
