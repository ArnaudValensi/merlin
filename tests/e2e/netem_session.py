"""A whole streaming session on a bad network, inside an unprivileged network
namespace (run by test_app_network.py through ``unshare -rn``).

The namespace has its own loopback and a dummy interface (10.99.0.1, so ICE
has a candidate: browsers and libnice skip loopback); traffic to it goes
through ``lo``, where ``tc netem`` adds loss and delay. A throwaway Merlin
(its own home, no SaaS token), an app drawing a moving ball, and headless
Chromium all run inside. Prints one JSON object: what the chip showed each
second of each phase, the stream's final state, and the streamer's ``ice:``,
``rate:`` and ``session:`` log lines.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
CLEAN_S, LOSSY_S, RECOVER_S = 6, 20, 25
NETEM = ("loss", "15%", "delay", "60ms")


def sh(*cmd: str) -> None:
    subprocess.run(cmd, check=True)


def main() -> int:
    sh("ip", "link", "set", "lo", "up")
    sh("ip", "link", "add", "d0", "type", "dummy")
    sh("ip", "addr", "add", "10.99.0.1/24", "dev", "d0")
    sh("ip", "link", "set", "d0", "up")

    home = Path(tempfile.mkdtemp(prefix="netem-home-"))
    (home / "config.env").write_text("")
    user = Path(tempfile.mkdtemp(prefix="netem-user-"))
    (user / ".zshrc").write_text("")
    drop = ("MERLIN_SAAS_TOKEN", "MERLIN_ENVIRONMENT_SLUG", "TMUX", "VIRTUAL_ENV")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(
        MERLIN_HOME=str(home),
        HOME=str(user),
        MERLIN_DEV="1",
        MERLIN_FEATURES="app",
        DASHBOARD_PASS="",
        DISCORD_BOT_TOKEN="",
        TMUX_TMPDIR=tempfile.mkdtemp(prefix="nt-"),
        UV_OFFLINE="1",  # no network in here: uv must not look for one
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = subprocess.Popen(
        ["uv", "run", "main.py", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    out: dict = {"chips": []}
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/apps", timeout=2)
                break
            except OSError:
                time.sleep(0.5)
        launched = subprocess.run(
            [
                str(ROOT / "app" / "commands" / "run.py"),
                "--name",
                "ball",
                "--gpu",
                "off",
                "--audio",
                "local",
                "--",
                "gst-launch-1.0",
                "-q",
                "videotestsrc",
                "pattern=ball",
                "!",
                "video/x-raw,width=1280,height=720",
                "!",
                "ximagesink",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        out["launch"] = json.loads(launched.stdout or "{}").get("status")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(f"http://127.0.0.1:{port}/apps/ball/play")
            page.wait_for_function(
                "document.getElementById('player').dataset.streamState === 'live'",
                timeout=30000,
            )

            def sample(phase: str) -> None:
                time.sleep(1)
                text, level, state = page.evaluate(
                    """() => {
                        const chip = document.getElementById('player-chip');
                        const gauge = chip.querySelector('.stream-gauge');
                        return [chip.textContent, gauge ? +gauge.dataset.level : null,
                                document.getElementById('player').dataset.streamState];
                    }"""
                )
                out["chips"].append([phase, text, level, state])

            for _ in range(CLEAN_S):
                sample("clean")
            sh("tc", "qdisc", "add", "dev", "lo", "root", "netem", *NETEM)
            for _ in range(LOSSY_S):
                sample("lossy")
            shots = Path("/tmp/merlin-app-shots")
            shots.mkdir(exist_ok=True)
            page.locator("#player-chip").screenshot(path=str(shots / "chip-lossy.png"))
            sh("tc", "qdisc", "del", "dev", "lo", "root")
            for _ in range(RECOVER_S):
                sample("recover")
            browser.close()
        time.sleep(2)  # the streamer logs its summary when the viewer leaves
    finally:
        subprocess.run(
            [str(ROOT / "app" / "commands" / "stop.py"), "ball"],
            env=env,
            capture_output=True,
            check=False,
        )
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
    log = home / "logs" / "merlin.log"
    lines = log.read_text().splitlines() if log.exists() else []
    out["log"] = [
        line.split("streamer: ", 1)[1]
        for line in lines
        if "streamer: " in line
        and any(k in line for k in ("ice: route", "rate: ", "session: "))
    ]
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
