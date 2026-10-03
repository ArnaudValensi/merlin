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
            "keyup Escape",
            "keydown Right",
            "keyup Right",
            "keydown Up",
            "keyup Up",
        ],
    )
    assert lines.index("keydown x") < lines.index("keyup x")


def _lines_with(log, prefix, count=1, timeout=5.0):
    """Wait for at least ``count`` probe lines starting with ``prefix``."""
    deadline = time.monotonic() + timeout
    hits = []
    while time.monotonic() < deadline:
        lines = log.read_text().splitlines() if log.exists() else []
        hits = [line for line in lines if line.startswith(prefix)]
        if len(hits) >= count:
            return hits
        time.sleep(0.1)
    raise AssertionError(f"wanted {count} '{prefix}' lines, got {hits}")


def test_trackpad_tap_and_two_finger_tap(phone):
    page, log = phone
    page.evaluate("window.MerlinPlayer.setProfile('trackpad')")
    assert page.locator(".player-pad-btn").count() == 0
    x, y = _center(page, "#player")
    page.touchscreen.tap(x, y)
    _lines_with(log, "btnup 1 ")
    page.wait_for_timeout(400)  # past the double-tap-drag window
    _touch(page, [(x - 40, y), (x + 40, y)])
    _lines_with(log, "btnup 3 ")


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
    second = _lines_with(log, "btndown 1 ", count=2)[1].split()
    assert int(second[2]) > int(first[2]) + 20  # moved right
    assert int(second[3]) > int(first[3]) + 10  # and down


def test_touch_profile_clicks_where_you_tap(phone):
    page, log = phone
    page.evaluate("window.MerlinPlayer.setProfile('touch')")
    px, py = _display_point(page, 300, 200)
    page.touchscreen.tap(px, py)
    _lines_with(log, "btnup 1 ")
    hits = _lines_with(log, "btndown 1 ")
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
    wait_for_lines(log, ["keydown Escape", "keyup Escape", "keydown Tab", "keyup Tab"])
    page.tap("#player-keys [data-action='keyboard-close']")
    page.wait_for_selector("#player-keys", state="hidden")


def _chip_until(page, condition: str, timeout: int) -> None:
    """Wait for the chip, and say what it showed if it never does."""
    try:
        page.wait_for_function(condition, timeout=timeout)
    except Exception as exc:
        chip, state = page.evaluate(
            "[document.getElementById('player-chip').textContent,"
            " document.getElementById('player').dataset.streamState]"
        )
        raise AssertionError(f"chip {chip!r}, stream {state!r}") from exc


def test_agent_input_shows_in_the_chip(merlin, phone):
    page, _ = phone
    assert cli(merlin, "input", "probe", "key", "x").returncode == 0
    _chip_until(
        page,
        "document.getElementById('player-chip').textContent.includes('agent')",
        5000,
    )
    _chip_until(
        page,
        "document.getElementById('player-chip').textContent.startsWith('LAN')",
        8000,
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


def test_second_finger_during_a_trackpad_drag_releases_the_button(phone):
    page, log = phone
    page.evaluate("window.MerlinPlayer.setProfile('trackpad')")
    x, y = _center(page, "#player")
    page.touchscreen.tap(x, y)
    _lines_with(log, "btnup 1 ")
    # Double-tap-and-hold starts a drag (button down)...
    cdp = page.context.new_cdp_session(page)
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 0}]},
    )
    _lines_with(log, "btndown 1 ", count=2)
    # ...then a second finger lands: the drag must let go of the button.
    cdp.send(
        "Input.dispatchTouchEvent",
        {
            "type": "touchStart",
            "touchPoints": [{"x": x, "y": y, "id": 0}, {"x": x + 80, "y": y, "id": 1}],
        },
    )
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    cdp.detach()
    _lines_with(log, "btnup 1 ", count=2)


def _hold(page, x, y):
    cdp = page.context.new_cdp_session(page)
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 0}]},
    )
    return cdp


def test_switching_profile_releases_held_gamepad_keys(phone):
    page, log = phone
    ax, ay = _center(page, ".player-pad-btn[data-button='A']")
    cdp = _hold(page, ax, ay)
    wait_for_lines(log, ["keydown x"])
    page.evaluate("window.MerlinPlayer.setProfile('trackpad')")
    wait_for_lines(log, ["keydown x", "keyup x"])
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    cdp.detach()

    page.evaluate("window.MerlinPlayer.setProfile('gamepad')")
    box = page.locator(".player-dpad").bounding_box()
    assert box
    cdp = _hold(page, box["x"] + box["width"] - 10, box["y"] + box["height"] / 2)
    wait_for_lines(log, ["keydown Right"])
    page.evaluate("window.MerlinPlayer.setProfile('touch')")
    wait_for_lines(log, ["keydown Right", "keyup Right"])
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    cdp.detach()


def test_nothing_on_the_player_is_selectable(phone):
    """Fast taps on the controls must not select their labels."""
    page, _ = phone
    styles = page.evaluate(
        """() => ['.player-pad-btn', '.player-dpad', '#player-chip', '#player-menu-btn', 'body']
            .map((sel) => {
                const s = getComputedStyle(document.querySelector(sel));
                return [sel, s.userSelect || s.webkitUserSelect];
            })"""
    )
    assert all(value == "none" for _, value in styles), styles
    # Double-tapping a button label selects nothing.
    page.locator(".player-pad-btn[data-button='A'] small").dblclick()
    assert page.evaluate("window.getSelection().toString()") == ""
    kb = page.evaluate(
        "getComputedStyle(document.getElementById('player-kb')).userSelect"
    )
    assert kb == "text"


def _quitter(merlin) -> None:
    """An app that writes a colored line, shows a window, and exits."""
    from app_stream_support import PROBE

    result = cli(
        merlin,
        "run",
        "--name",
        "quitter",
        "--gpu",
        "off",
        "--",
        "sh",
        "-c",
        f"printf '\\033[0;93mhello from the app\\033[0m\\n'; exec {PROBE} --exit-after 4",
    )
    assert result.returncode == 0, result.stderr


def _wait_exited(page) -> None:
    page.wait_for_function(
        "document.getElementById('player').dataset.streamState === 'exited'",
        timeout=20000,
    )


def test_an_ended_app_offers_its_logs_and_a_way_out(
    merlin, playwright, browser, server
):
    """On a phone the logs open over the player (a new tab has no way back
    in full screen), Back closes them, and Leave returns to the page the
    player was opened from."""
    _quitter(merlin)
    context = browser.new_context(**playwright.devices["Pixel 7 landscape"])
    page = context.new_page()
    try:
        page.goto(f"{server}/jobs")
        page.evaluate("location.assign('/apps/quitter/play')")  # as a link would
        page.wait_for_url("**/apps/quitter/play")
        _wait_exited(page)
        buttons = page.locator("#player-status button")
        assert buttons.all_inner_texts() == ["Logs", "Leave"]
        url = page.url

        page.tap("#player-status button:has-text('Logs')")
        page.wait_for_selector("#player-logs:not([hidden])")
        page.wait_for_function(
            "document.getElementById('player-logs-text').textContent.includes('hello from the app')"
        )
        text = page.inner_text("#player-logs-text")
        assert "\x1b" not in text and "[0;93m" not in text
        assert len(context.pages) == 1 and page.url == url  # no tab, no navigation
        SHOTS.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOTS / "player-logs.png"))

        page.tap("#player-logs-back")
        page.wait_for_selector("#player-logs", state="hidden")
        assert page.url == url
        assert "exited" in page.inner_text("#player-status")

        page.tap("#player-status button:has-text('Leave')")
        page.wait_for_url("**/jobs", timeout=10000)
    finally:
        context.close()
        stop_all(merlin)


def test_the_end_screen_works_from_the_keyboard(merlin, browser, server):
    """On a desktop the keys stop going to an app that is not playing: Tab
    reaches the end screen, the logs keep the keyboard while open, Escape
    returns to Logs, and Leave (opened directly: no page to go back to) goes
    to the Apps page."""
    _quitter(merlin)
    context = browser.new_context(viewport={"width": 1280, "height": 720})
    page = context.new_page()
    active = "document.activeElement && document.activeElement.textContent.trim()"
    in_logs = "document.getElementById('player-logs').contains(document.activeElement)"
    try:
        page.goto(f"{server}/apps/quitter/play")
        _wait_exited(page)
        page.locator("#player").focus()
        page.keyboard.press("Tab")
        assert page.evaluate(active) == "Logs"
        page.keyboard.press("Enter")
        page.wait_for_selector("#player-logs:not([hidden])")
        assert page.evaluate(active) == "‹ Back"
        for key in ("Tab", "Tab", "Tab", "Shift+Tab", "Shift+Tab", "Shift+Tab"):
            page.keyboard.press(key)
            focus = page.evaluate(
                "document.activeElement === document.body || " + in_logs
            )
            assert focus, f"focus left the logs after {key}"
        page.keyboard.press("Escape")
        page.wait_for_selector("#player-logs", state="hidden")
        assert page.evaluate(active) == "Logs"
        page.keyboard.press("Tab")
        assert page.evaluate(active) == "Leave"
        page.keyboard.press("Enter")
        page.wait_for_url("**/apps", timeout=10000)
    finally:
        context.close()
        stop_all(merlin)


def test_the_logs_cover_the_iphone_hint(merlin, playwright, browser, server):
    """On an iPhone outside the Home Screen the hint is showing; while the
    logs are open it is inert, so it must not sit over them."""
    _quitter(merlin)
    device = {
        k: v
        for k, v in playwright.devices["iPhone 13"].items()
        if k != "default_browser_type"
    }
    context = browser.new_context(**device)
    page = context.new_page()
    try:
        page.goto(f"{server}/apps/quitter/play")
        _wait_exited(page)
        assert page.locator("#player-hint").is_visible()
        page.tap("#player-status button:has-text('Logs')")
        page.wait_for_selector("#player-logs:not([hidden])")
        # Both are fixed children of <body>, one stacking context: the higher
        # z-index paints on top. (elementFromPoint cannot tell: it skips the
        # inert hint either way.)
        order = page.evaluate(
            """() => ['player-logs', 'player-hint'].map((id) => {
                const style = getComputedStyle(document.getElementById(id));
                return [style.position, parseInt(style.zIndex, 10)];
            })"""
        )
        (logs_pos, logs_z), (hint_pos, hint_z) = order
        assert logs_pos == hint_pos == "fixed"
        assert logs_z > hint_z, "the hint covers the logs"
        page.tap("#player-logs-back")
        page.wait_for_selector("#player-logs", state="hidden")
        page.tap("#player-hint-close")  # dismissable again once they close
        page.wait_for_selector("#player-hint", state="hidden")
    finally:
        context.close()
        stop_all(merlin)
