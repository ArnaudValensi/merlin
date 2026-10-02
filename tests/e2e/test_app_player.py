"""The full-screen player on a phone: gamepad, trackpad and touch profiles,
the key row, the agent-input indicator, and stopping from the sheet."""

import re
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
    wait_for_lines,
    wait_live,
)

MERLIN_OPTIONS = APP_OPTIONS
pytestmark = requires_streaming

SHOTS = Path("/tmp/merlin-app-shots")
KEYS = "A=x,B=z,X=r,Y=Tab,Start=Escape"


@pytest.fixture(scope="module")
def playwright():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright):
    browser = playwright.chromium.launch()
    yield browser
    browser.close()


@pytest.fixture
def probe(merlin, tmp_path):
    log = tmp_path / "probe.log"
    handle = launch_probe(merlin, log, "probe", "--controls", "gamepad", "--keys", KEYS)
    yield handle, log
    stop_all(merlin)


@pytest.fixture
def phone(playwright, browser, server, probe):
    context = browser.new_context(**playwright.devices["Pixel 7 landscape"])
    page = context.new_page()
    page.goto(f"{server}/apps/probe/play")
    wait_live(page)
    page.wait_for_function("window.MerlinPlayer.profile === 'gamepad'")
    yield page, probe[1]
    context.close()


def _center(page, selector):
    box = page.locator(selector).bounding_box()
    assert box
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _touch(page, points_down, points_up=None):
    """Multi-finger touch through CDP (Playwright only taps with one finger)."""
    cdp = page.context.new_cdp_session(page)
    touch = [{"x": x, "y": y, "id": i} for i, (x, y) in enumerate(points_down)]
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": touch})
    if points_up:
        moved = [{"x": x, "y": y, "id": i} for i, (x, y) in enumerate(points_up)]
        cdp.send(
            "Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": moved}
        )
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    cdp.detach()


def _display_point(page, x, y):
    """Viewport point for display pixel (x, y) of the contained 1280x720 video."""
    return page.evaluate(
        """([x, y]) => {
            const v = document.getElementById('player-video');
            const r = v.getBoundingClientRect();
            const s = Math.min(r.width / v.videoWidth, r.height / v.videoHeight);
            return [r.left + (r.width - v.videoWidth * s) / 2 + x * s,
                    r.top + (r.height - v.videoHeight * s) / 2 + y * s];
        }""",
        [x, y],
    )


def test_gamepad_buttons_and_dpad(phone):
    page, log = phone
    SHOTS.mkdir(exist_ok=True)
    page.wait_for_timeout(600)
    page.screenshot(path=str(SHOTS / "player-gamepad.png"))

    assert page.locator(".player-pad-btn").count() == 5
    page.locator(".player-pad-btn[data-button='A']").tap()
    page.locator(".player-pad-btn[data-button='Start']").tap()
    box = page.locator(".player-dpad").bounding_box()
    assert box
    page.touchscreen.tap(box["x"] + box["width"] - 10, box["y"] + box["height"] / 2)
    page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + 8)
    lines = wait_for_lines(
        log,
        [
            "keydown x",
            "keyup x",
            "keydown Escape",
            "keydown Right",
            "keyup Right",
            "keydown Up",
        ],
    )
    assert "keyup Up" in wait_for_lines(log, ["keyup Up"])
    assert lines.index("keydown x") < lines.index("keyup x")


def _lines_with(log, prefix, timeout=5.0):
    lines = []
    for _ in range(int(timeout * 10)):
        lines = log.read_text().splitlines() if log.exists() else []
        hits = [line for line in lines if line.startswith(prefix)]
        if hits:
            return hits
        time.sleep(0.1)
    return []


def test_trackpad_tap_and_two_finger_tap(phone):
    page, log = phone
    page.evaluate("window.MerlinPlayer.setProfile('trackpad')")
    assert page.locator(".player-pad-btn").count() == 0
    x, y = _center(page, "#player")
    page.touchscreen.tap(x, y)
    assert _lines_with(log, "btndown 1 ")
    page.wait_for_timeout(400)  # past the double-tap-drag window
    _touch(page, [(x - 40, y), (x + 40, y)])
    assert _lines_with(log, "btndown 3 ")


def test_trackpad_drag_moves_the_cursor(phone):
    page, log = phone
    page.evaluate("window.MerlinPlayer.setProfile('trackpad')")
    x, y = _center(page, "#player")
    page.touchscreen.tap(x, y)  # click where the cursor is now
    first = _lines_with(log, "btndown 1 ")[0].split()
    page.wait_for_timeout(400)  # past the double-tap-drag window
    _touch(page, [(x, y)], [(x + 60, y + 30)])
    page.wait_for_timeout(300)
    page.touchscreen.tap(x, y)
    deadline = time.monotonic() + 5
    clicks = []
    while time.monotonic() < deadline:
        clicks = _lines_with(log, "btndown 1 ")
        if len(clicks) >= 2:
            break
        time.sleep(0.1)
    second = clicks[1].split()
    assert int(second[2]) > int(first[2]) + 20  # moved right
    assert int(second[3]) > int(first[3]) + 10  # and down


def test_touch_profile_clicks_where_you_tap(phone):
    page, log = phone
    page.evaluate("window.MerlinPlayer.setProfile('touch')")
    px, py = _display_point(page, 300, 200)
    page.touchscreen.tap(px, py)
    hits = _lines_with(log, "btndown 1 ")
    assert hits
    _, _, bx, by = hits[0].split()
    assert abs(int(bx) - 300) <= 3 and abs(int(by) - 200) <= 3


def test_pinch_zooms_the_view(phone):
    page, _ = phone
    x, y = _center(page, "#player")
    _touch(page, [(x - 30, y), (x + 30, y)], [(x - 120, y), (x + 120, y)])
    assert page.evaluate("window.MerlinPlayer.zoom") > 1.5
    page.click("#player-menu-btn")
    page.click("[data-action='reset-zoom']")
    assert page.evaluate("window.MerlinPlayer.zoom") == 1


def test_key_row_sends_special_keys(phone):
    page, log = phone
    page.tap("#player-menu-btn")
    page.tap("[data-action='keyboard']")
    page.wait_for_selector("#player-keys:not([hidden])")
    page.tap("#player-keys [data-key='Escape']")
    page.tap("#player-keys [data-key='Tab']")
    lines = wait_for_lines(log, ["keydown Escape", "keydown Tab"])
    assert "keydown Tab" in lines
    page.tap("#player-keys [data-action='keyboard-close']")
    page.wait_for_selector("#player-keys", state="hidden")


def test_agent_input_shows_in_the_chip(merlin, phone):
    page, _ = phone
    assert cli(merlin, "input", "probe", "key", "x").returncode == 0
    page.wait_for_function(
        "document.getElementById('player-chip').textContent.includes('agent')",
        timeout=5000,
    )
    page.wait_for_function(
        "document.getElementById('player-chip').textContent.startsWith('LAN')",
        timeout=8000,
    )


def test_stop_from_the_sheet(phone):
    page, _ = phone
    page.tap("#player-menu-btn")
    page.tap("[data-action='stop']")
    assert re.search("Tap again", page.inner_text("[data-action='stop']"))
    page.tap("[data-action='stop']")
    page.wait_for_function(
        "document.getElementById('player').dataset.streamState === 'exited'",
        timeout=10000,
    )
    assert "stopped" in page.inner_text("#player-status")


def test_portrait_screenshot(playwright, browser, server, probe):
    context = browser.new_context(**playwright.devices["Pixel 7"])
    page = context.new_page()
    try:
        page.goto(f"{server}/apps/probe/play")
        wait_live(page)
        page.tap("#player-menu-btn")
        page.wait_for_timeout(500)
        SHOTS.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOTS / "player-portrait-sheet.png"))
    finally:
        context.close()
