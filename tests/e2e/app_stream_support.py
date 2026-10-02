"""Shared helpers for the app-streaming E2E tests.

The probe app (tests/fixtures/x_probe.py) is launched with the CLI against
the throwaway server's own MERLIN_HOME, so the server sees it like an app an
agent started. Skipped when the streaming prerequisites are missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "tests" / "fixtures" / "x_probe.py"
COMMANDS = ROOT / "app" / "commands"
APP_OPTIONS = {"extra_env": {"MERLIN_FEATURES": "app"}}


def _system_python_ok() -> bool:
    probe = (
        "import gi; gi.require_version('GstWebRTC', '1.0'); "
        "from gi.repository import GstWebRTC; import Xlib"
    )
    try:
        return (
            subprocess.run(
                ["/usr/bin/python3", "-c", probe], capture_output=True, check=False
            ).returncode
            == 0
        )
    except OSError:
        return False


requires_streaming = pytest.mark.skipif(
    not (shutil.which("Xvfb") and shutil.which("xdotool") and _system_python_ok()),
    reason="needs Xvfb, xdotool, GStreamer WebRTC and python-xlib",
)


def cli(merlin, command: str, *args: str, extra_env: dict | None = None):
    env = dict(merlin.env)
    env.update(extra_env or {})
    return subprocess.run(
        [str(COMMANDS / f"{command}.py"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def launch_probe(
    merlin,
    log: Path,
    name: str = "probe",
    *extra: str,
    env: dict | None = None,
    probe_args: tuple[str, ...] = (),
) -> dict:
    result = cli(
        merlin,
        "run",
        "--name",
        name,
        "--gpu",
        "off",
        *extra,
        "--",
        str(PROBE),
        *probe_args,
        extra_env={"X_PROBE_LOG": str(log), **(env or {})},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def stop_all(merlin) -> None:
    listed = cli(merlin, "list")
    for record in json.loads(listed.stdout or "[]"):
        cli(merlin, "stop", record["id"])


def wait_for_lines(log: Path, expected: list[str], timeout: float = 10) -> list[str]:
    """Wait until every expected line is in the probe log; fail naming the
    missing ones otherwise (a timeout must never pass silently)."""
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    while time.monotonic() < deadline:
        lines = log.read_text().splitlines() if log.exists() else []
        if all(line in lines for line in expected):
            return lines
        time.sleep(0.05)
    missing = [line for line in expected if line not in lines]
    raise AssertionError(f"probe never logged {missing}; got {lines[-20:]}")


def streamer_pids(display: str) -> list[int]:
    out = subprocess.run(
        ["pgrep", "-f", f"streamer.py --display {display} "],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [int(pid) for pid in out.split() if int(pid) != os.getpid()]


def wait_live(page, root: str = "#player", timeout: float = 30000) -> None:
    page.wait_for_function(
        """(sel) => {
            const root = document.querySelector(sel);
            const video = root && root.querySelector('video');
            return root && root.dataset.streamState === 'live' && video &&
                video.videoWidth > 0 && video.readyState >= 2;
        }""",
        arg=root,
        timeout=timeout,
    )


def pixel(page, x: int, y: int, selector: str = "#player-video") -> list[int]:
    return page.evaluate(
        """([sel, x, y]) => {
            const v = document.querySelector(sel);
            const c = document.createElement('canvas');
            c.width = v.videoWidth; c.height = v.videoHeight;
            const g = c.getContext('2d');
            g.drawImage(v, 0, 0);
            return Array.from(g.getImageData(x, y, 1, 1).data).slice(0, 3);
        }""",
        [selector, x, y],
    )


def close_to(actual: list[int], expected: tuple[int, int, int], tol: int = 48) -> bool:
    return all(abs(a - e) <= tol for a, e in zip(actual, expected))
