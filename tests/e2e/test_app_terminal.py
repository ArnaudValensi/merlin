"""Apps in the web terminal: the ▶ button for apps started from the current
tmux window, the toast, the docked desktop panel and the mobile mini-player."""

import json
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from app_stream_support import (  # noqa: E402
    APP_OPTIONS,
    cli,
    launch_probe,
    requires_streaming,
    stop_all,
    wait_live,
)

MERLIN_OPTIONS = APP_OPTIONS
pytestmark = requires_streaming

SHOTS = Path("/tmp/merlin-app-shots")


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


def _terminal(browser, server, **context_args):
    context = browser.new_context(**context_args)
    page = context.new_page()
    page.goto(f"{server}/terminal")
    page.wait_for_function(
        "() => window.MerlinTerminal && window.MerlinTerminal.currentWindow()",
        timeout=20000,
    )
    return context, page


def _pane_of(merlin, window_id: str) -> str:
    out = subprocess.run(
        ["tmux", "list-panes", "-t", window_id, "-F", "#{pane_id}"],
        env=merlin.env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return out[0]


def _launch_from(merlin, page, tmp_path, name="probe") -> dict:
    window = page.evaluate("window.MerlinTerminal.currentWindow()")
    pane = _pane_of(merlin, window)
    return launch_probe(merlin, tmp_path / f"{name}.log", name, env={"TMUX_PANE": pane})


def test_button_toast_and_docked_panel(merlin, browser, server, tmp_path):
    context, page = _terminal(browser, server, viewport={"width": 1400, "height": 820})
    try:
        page.wait_for_timeout(1000)  # the first poll records existing apps
        handle = _launch_from(merlin, page, tmp_path)
        assert handle["origin"]["kind"] == "terminal"

        page.wait_for_selector("#app-btn:not([hidden])", timeout=10000)
        assert page.inner_text("#app-btn-label") == "probe"
        page.wait_for_selector(".app-toast.show", timeout=10000)
        assert "probe started in" in page.inner_text(".app-toast")

        page.click(".app-toast button")
        page.wait_for_selector("#app-panel:not([hidden])")
        wait_live(page, ".app-panel-body")
        assert page.evaluate(
            "document.querySelector('.main').classList.contains('app-open')"
        )
        box = page.locator("#app-panel").bounding_box()
        assert box and box["x"] > 600 and box["height"] > 400  # docked on the right
        SHOTS.mkdir(exist_ok=True)
        page.wait_for_timeout(800)
        page.screenshot(path=str(SHOTS / "terminal-desktop.png"))

        # Opening Sessions takes the docked slot back: the app panel closes.
        page.click("#sessions-btn")
        page.wait_for_selector("#app-panel", state="hidden", timeout=5000)
        listed = json.loads(cli(merlin, "list").stdout)
        assert [a["status"] for a in listed] == ["running"]

        # The button reopens it; ✕ closes it and the app keeps running.
        page.click("#app-btn")
        wait_live(page, ".app-panel-body")
        page.click("#app-panel .app-icon-btn[title^='Close']")
        page.wait_for_selector("#app-panel", state="hidden")
        assert json.loads(cli(merlin, "list").stdout)[0]["status"] == "running"
    finally:
        context.close()


def test_button_only_on_the_launching_window(merlin, browser, server, tmp_path):
    context, page = _terminal(browser, server, viewport={"width": 1400, "height": 820})
    try:
        _launch_from(merlin, page, tmp_path)
        page.wait_for_selector("#app-btn:not([hidden])", timeout=10000)
        first = page.evaluate("window.MerlinTerminal.currentWindow()")
        session = page.evaluate("window.MerlinTerminal.currentSession()")
        new_window = subprocess.run(
            [
                "tmux",
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id}",
                "-t",
                f"{session}:",
            ],
            env=merlin.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        page.evaluate(f"window.MerlinTerminal.switchSession('{session}:{new_window}')")
        page.wait_for_function(
            f"window.MerlinTerminal.currentWindow() === '{new_window}'", timeout=10000
        )
        page.wait_for_selector("#app-btn", state="hidden", timeout=5000)
        page.evaluate(f"window.MerlinTerminal.switchSession('{session}:{first}')")
        page.wait_for_selector("#app-btn:not([hidden])", timeout=5000)
    finally:
        context.close()


def test_several_apps_show_a_chooser(merlin, browser, server, tmp_path):
    context, page = _terminal(browser, server, viewport={"width": 1400, "height": 820})
    try:
        _launch_from(merlin, page, tmp_path, "one")
        _launch_from(merlin, page, tmp_path, "two")
        page.wait_for_selector("#app-btn-count:not([hidden])", timeout=10000)
        assert page.inner_text("#app-btn-count") == "2"
        page.click("#app-btn")
        page.wait_for_selector(".app-chooser")
        assert page.locator(".app-chooser button").count() == 2
        page.click(".app-chooser button:has-text('one')")
        wait_live(page, ".app-panel-body")
        assert page.inner_text(".app-panel-name") == "one"
    finally:
        context.close()


def test_mobile_mini_player(merlin, playwright, browser, server, tmp_path):
    device = playwright.devices["Pixel 7"]
    context, page = _terminal(browser, server, **device)
    try:
        _launch_from(merlin, page, tmp_path)
        page.wait_for_selector("#app-btn:not([hidden])", timeout=10000)
        page.tap("#app-btn")
        page.wait_for_selector(".app-panel.mini")
        wait_live(page, ".app-panel-body")
        assert page.locator(".app-toast.show").count() == 0
        box = page.locator("#app-panel").bounding_box()
        viewport = page.viewport_size
        assert box and viewport
        assert box["width"] < viewport["width"] * 0.6  # floating, not full width
        assert box["x"] + box["width"] > viewport["width"] / 2  # bottom-right corner
        SHOTS.mkdir(exist_ok=True)
        page.wait_for_timeout(800)
        page.screenshot(path=str(SHOTS / "terminal-mobile.png"))

        # Drag it to the top-left corner: it snaps there and remembers.
        start = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.move(*start)
        page.mouse.down()
        page.mouse.move(40, 120, steps=8)
        page.mouse.up()
        page.wait_for_function(
            "document.getElementById('app-panel').dataset.corner === 'tl'"
        )
        assert page.evaluate("localStorage.getItem('app-mini-corner')") == "tl"

        # A tap opens the full-screen player.
        page.locator(".app-panel-body").tap()
        page.wait_for_url("**/apps/probe/play", timeout=10000)
    finally:
        context.close()


def test_stopping_the_app_tells_the_panel(merlin, browser, server, tmp_path):
    context, page = _terminal(browser, server, viewport={"width": 1400, "height": 820})
    try:
        _launch_from(merlin, page, tmp_path)
        page.wait_for_selector("#app-btn:not([hidden])", timeout=10000)
        page.click("#app-btn")
        wait_live(page, ".app-panel-body")
        cli(merlin, "stop", "probe")
        page.wait_for_selector(".app-panel-overlay:not([hidden])", timeout=10000)
        assert "stopped" in page.inner_text(".app-panel-overlay")
        time.sleep(0.2)
    finally:
        context.close()
