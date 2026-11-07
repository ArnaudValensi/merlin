"""The sweep's death detection against a real, isolated tmux server.

The server lives on a private ``TMUX_TMPDIR`` and is killed by its socket
only (``-S``), never through an environment variable: see
``tests/tmux_isolation.py`` and ``docs/dev/development-setup.md``. Each test
plays the 2026-11-05 loss: a sweep, the server dies, a sweep.
"""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from workspace import service, store
from workspace import snapshot as snap

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


@pytest.fixture
def iso(monkeypatch, tmp_path):
    sockdir = Path(tempfile.mkdtemp(prefix="wsd-"))  # short: unix socket path limit
    for var in ("TMUX", "TMUX_PANE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(sockdir))
    monkeypatch.setenv("MERLIN_HOME", str(tmp_path / "merlin"))
    yield sockdir
    kill(sockdir)
    shutil.rmtree(sockdir, ignore_errors=True)


def kill(sockdir: Path) -> None:
    """Kill the private server by its socket, and wait until nothing answers
    on it any more (tmux reports a leaving server for a moment)."""
    sock = sockdir / f"tmux-{os.getuid()}" / "default"
    subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)
    for _ in range(100):
        r = subprocess.run(
            ["tmux", "-S", str(sock), "list-sessions"], capture_output=True, text=True
        )
        if "no server running" in r.stderr or "error connecting" in r.stderr:
            return
        time.sleep(0.02)


def tmux(*args):
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=False
    ).stdout.strip()


def start(*names):
    for name in names:
        tmux("new-session", "-d", "-s", name)
    time.sleep(0.2)


def identity():
    rows = snap.read_tmux()
    assert rows is not None
    server = snap.server_identity(rows[0])
    assert server is not None
    return server["pid"], server["start_time"]


def crash_points():
    return [store.read_snapshot(p) for p in sorted(store.crashes_dir().glob("*.json"))]


def test_server_changed_freezes_the_old_workspace_then_writes_the_new(iso):
    start("alpha", "beta")
    state = service.sweep_once(service.SweepState())
    old = identity()
    assert store.server_of(state.last) == old
    assert [s["name"] for s in state.last["sessions"]] == ["alpha", "beta"]

    kill(iso)
    start("merlin-dev")  # the web terminal recreating a server at once
    new = identity()
    assert new != old
    service.sweep_once(state, now=1234.5)

    (point,) = crash_points()
    assert point["crash"] == {"reason": "server-changed", "detected_at": 1234.5}
    assert store.server_of(point) == old
    offer = store.read_snapshot(store.pending_path())
    assert [s["name"] for s in offer["sessions"]] == ["alpha", "beta"]
    assert offer["id"] == store.entry_path(offer["id"]).stem
    latest = store.read_snapshot(store.latest_path())
    assert store.server_of(latest) == new
    assert [s["name"] for s in latest["sessions"]] == ["merlin-dev"]
    # Once: the next sweep freezes nothing more.
    service.sweep_once(state)
    assert len(crash_points()) == 1


def test_server_gone_takes_two_sweeps_and_leaves_latest_alone(iso):
    start("alpha")
    state = service.sweep_once(service.SweepState())
    old = store.read_snapshot(store.latest_path())

    kill(iso)
    service.sweep_once(state)
    assert crash_points() == [] and not store.pending_path().exists()
    service.sweep_once(state, now=99.0)
    (point,) = crash_points()
    assert point["crash"] == {"reason": "server-gone", "detected_at": 99.0}
    assert store.read_snapshot(store.pending_path())["sessions"] == old["sessions"]
    assert store.read_snapshot(store.latest_path()) == old
    service.sweep_once(state)
    assert len(crash_points()) == 1
