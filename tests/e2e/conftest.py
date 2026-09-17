"""One way to start a throwaway Merlin for the E2E suite.

Every server the suite starts runs on its own home (``MERLIN_HOME`` in a temp
dir with a one-line ``config.env``), its own tmux server (a private
``TMUX_TMPDIR``, ``$TMUX`` unset so the spawned ``tmux new-session`` never
refuses to nest), a random port, auth off unless a password is given, and no
SaaS or Discord token unless given. Nothing a test does can reach ``~/.merlin``
(config, jobs, logs, the live server's state file) or the real tmux server.

Use the ``server`` fixture (the URL) and ``tmux_env`` (an environment for
``tmux`` commands against that server's private socket). A module that needs
options sets ``MERLIN_OPTIONS`` at module level, a dict of ``start_merlin``
keyword arguments. A module that needs several servers calls ``start_merlin``
and ``stop_merlin`` from its own fixtures.
"""

import os
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@dataclass
class Merlin:
    url: str
    proc: subprocess.Popen
    home: Path
    env: dict = field(repr=False)


def start_merlin(
    tmp_path_factory,
    *,
    password: str = "",
    saas_token: str = "",
    config: str = "",
    extra_env: dict | None = None,
    ready: str = "/terminal",
    name: str = "merlin",
) -> Merlin:
    """Start a throwaway Merlin and wait until ``ready`` answers (a redirect
    to the login page counts, so the probe works with auth on)."""
    home = tmp_path_factory.mktemp(f"{name}-home")
    (home / "config.env").write_text(f"DASHBOARD_PASS={password}\n{config}")
    env = os.environ.copy()
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = str(tmp_path_factory.mktemp(f"{name}-tmux"))
    env.update(
        {
            "DASHBOARD_PASS": password,
            "MERLIN_SAAS_TOKEN": saas_token,
            "DISCORD_BOT_TOKEN": "",
            "DISCORD_CHANNEL_IDS": "",
            "MERLIN_HOME": str(home),
            "MERLIN_DEV": "1",
        }
    )
    env.update(extra_env or {})
    port = free_port()
    proc = subprocess.Popen(
        ["uv", "run", "main.py", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://localhost:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{url}{ready}", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("Merlin failed to start")
    return Merlin(url, proc, home, env)


def stop_merlin(server: Merlin) -> None:
    server.proc.send_signal(signal.SIGTERM)
    try:
        server.proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.proc.kill()
    subprocess.run(["tmux", "kill-server"], env=server.env, capture_output=True)


@pytest.fixture(scope="module")
def merlin(request, tmp_path_factory) -> Merlin:
    options = getattr(request.module, "MERLIN_OPTIONS", {})
    server = start_merlin(tmp_path_factory, **options)
    yield server
    stop_merlin(server)


@pytest.fixture(scope="module")
def server(merlin) -> str:
    return merlin.url


@pytest.fixture(scope="module")
def tmux_env(merlin) -> dict:
    return merlin.env
