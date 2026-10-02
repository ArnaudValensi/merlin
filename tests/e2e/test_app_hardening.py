"""Failure paths: the app crashing while watched, the streamer dying, Merlin
restarting mid-stream, and a machine without the prerequisites."""

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from app_stream_support import (  # noqa: E402
    APP_OPTIONS,
    PROBE,
    cli,
    launch_probe,
    requires_streaming,
    stop_all,
    streamer_pids,
    wait_live,
)
from conftest import ROOT, start_merlin, stop_merlin  # noqa: E402

MERLIN_OPTIONS = APP_OPTIONS
pytestmark = requires_streaming


@pytest.fixture(scope="module")
def playwright():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright):
    browser = playwright.chromium.launch()
    yield browser
    browser.close()


@pytest.fixture(autouse=True)
def _clean(merlin):
    yield
    stop_all(merlin)


def _state(page):
    return page.get_attribute("#player", "data-stream-state")


def _wait_state(page, state, timeout=15000):
    page.wait_for_function(
        f"document.getElementById('player').dataset.streamState === '{state}'",
        timeout=timeout,
    )


def test_app_crash_while_watched(merlin, browser, server, tmp_path):
    launch_probe(merlin, tmp_path / "p.log", "probe", "--no-fill")
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{server}/apps/probe/play")
        wait_live(page)
        record = json.loads(cli(merlin, "list").stdout)[0]
        os.kill(record["pid"], signal.SIGKILL)  # the wrapper shell: the app's exit
        _wait_state(page, "exited")
        assert "exited" in page.inner_text("#player-status")
        assert page.locator("#player-status button:has-text('Logs')").count() == 1
    finally:
        context.close()


def test_streamer_death_recovers_once_then_errors(merlin, browser, server, tmp_path):
    handle = launch_probe(merlin, tmp_path / "p.log")
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{server}/apps/probe/play")
        wait_live(page)
        [first] = streamer_pids(handle["display"])
        os.kill(first, signal.SIGKILL)
        # One automatic retry: live again, on a new streamer.
        page.wait_for_function(
            "document.getElementById('player').dataset.streamState !== 'live'",
            timeout=10000,
        )
        wait_live(page, timeout=20000)
        [second] = streamer_pids(handle["display"])
        assert second != first
        # A second death right after shows the error with a Retry button.
        os.kill(second, signal.SIGKILL)
        _wait_state(page, "error")
        page.click("#player-status button:has-text('Retry')")
        wait_live(page, timeout=20000)
    finally:
        context.close()


def test_merlin_restart_while_watching(tmp_path_factory, browser, tmp_path):
    server = start_merlin(tmp_path_factory, name="restart", **APP_OPTIONS)
    context = browser.new_context()
    try:
        handle = launch_probe(server, tmp_path / "p.log")
        page = context.new_page()
        page.goto(f"{server.url}/apps/probe/play")
        wait_live(page)

        # Stop the server (not its tmux, not the app) and start it again on the
        # same port and home, as `merlin restart` does.
        port = server.url.rsplit(":", 1)[1]
        server.proc.send_signal(signal.SIGTERM)
        server.proc.wait(timeout=15)
        assert json.loads(cli(server, "list").stdout)[0]["status"] == "running"
        server.proc = subprocess.Popen(
            ["uv", "run", "main.py", "--port", port],
            cwd=ROOT,
            env=server.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{server.url}/apps", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        wait_live(page, timeout=40000)
        after = json.loads(cli(server, "list").stdout)[0]
        assert after["pid"] == handle["pid"]
    finally:
        context.close()
        stop_all(server)
        stop_merlin(server)


@pytest.fixture(scope="module")
def no_xvfb_path(tmp_path_factory):
    """The real PATH with the directory holding Xvfb swapped for a copy
    without it (symlinks to everything else). Keeping every other directory
    as is matters: version-manager shims (pyenv) re-search PATH and would
    find themselves again in a flattened copy."""
    parts = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isfile(os.path.join(directory, "Xvfb")):
            parts.append(directory)
            continue
        copy = tmp_path_factory.mktemp("noxvfb-bin")
        for entry in os.scandir(directory):
            if entry.name != "Xvfb":
                (copy / entry.name).symlink_to(entry.path)
        parts.append(str(copy))
    path = os.pathsep.join(parts)
    assert shutil.which("Xvfb", path=path) is None
    return path


def test_missing_prerequisites(tmp_path_factory, browser, no_xvfb_path):
    options = {"extra_env": {**APP_OPTIONS["extra_env"], "PATH": no_xvfb_path}}
    server = start_merlin(tmp_path_factory, name="nodeps", **options)
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(f"{server.url}/apps")
        page.wait_for_selector("#apps-deps:not([hidden])", timeout=15000)
        assert "Xvfb" in page.inner_text("#apps-deps-list")
        assert "pacman" in page.inner_text("#apps-deps-install")

        result = cli(server, "run", "--", str(PROBE))
        assert result.returncode == 1
        assert "Missing Xvfb" in result.stderr

        page.goto(f"{server.url}/terminal")
        page.wait_for_function(
            "() => window.MerlinTerminal && window.MerlinTerminal.currentWindow()",
            timeout=20000,
        )
        page.wait_for_timeout(1500)
        assert page.locator("#app-btn").is_hidden()
    finally:
        context.close()
        stop_merlin(server)
