"""The Apps page: saved apps, launching into the player, thumbnails, stop,
edit, delete, agent-started apps, and fit-to-device on a phone."""

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from app_stream_support import (  # noqa: E402
    APP_OPTIONS,
    PROBE,
    ROOT,
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
    saved = Path(merlin.home) / "data" / "apps" / "saved.json"
    saved.unlink(missing_ok=True)


def _fill_form(page, name, size_mode="fixed", size="640x480", controls="trackpad"):
    page.fill("#apps-form [name=name]", name)
    page.fill("#apps-form [name=command]", str(PROBE))
    page.fill("#apps-form [name=cwd]", str(ROOT))
    page.select_option("#apps-form [name=controls]", controls)
    page.check(f"#apps-form [name=size_mode][value={size_mode}]")
    if size_mode == "fixed":
        page.fill("#apps-form [name=size]", size)
    page.click("#apps-form button[type=submit]")
    page.wait_for_selector("#apps-form-modal", state="hidden")


def test_saved_app_lifecycle(merlin, browser, server):
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(f"{server}/apps")
        page.wait_for_selector("#apps-empty:not([hidden])")
        page.click("#apps-add")
        _fill_form(page, "probe")
        page.wait_for_selector(".apps-card[data-id=probe]")
        assert page.inner_text(".apps-card[data-id=probe] .apps-status") == "Stopped"

        page.click(".apps-card[data-id=probe] button:has-text('Launch')")
        page.wait_for_url("**/apps/probe/play", timeout=20000)
        wait_live(page)
        assert page.evaluate(
            "[document.getElementById('player-video').videoWidth,"
            " document.getElementById('player-video').videoHeight]"
        ) == [640, 480]

        # Leave keeps it running; back on the page it can be resumed.
        page.click("#player-menu-btn")
        page.click("[data-action=leave]")
        page.wait_for_url(f"{server}/apps", timeout=10000)
        page.wait_for_selector(
            ".apps-card[data-id=probe] .apps-status.running", timeout=10000
        )
        page.wait_for_function(
            "getComputedStyle(document.querySelector('.apps-card[data-id=probe] .apps-thumb'))"
            ".backgroundImage.includes('thumb')",
            timeout=15000,
        )
        SHOTS.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOTS / "apps-desktop.png"))

        # Stop asks for a second click.
        stop = page.locator(".apps-card[data-id=probe] button:has-text('Stop')")
        stop.click()
        assert "Tap again" in stop.inner_text()
        stop.click()
        page.wait_for_selector(
            ".apps-card[data-id=probe] .apps-status.stopped", timeout=10000
        )
        assert json.loads(cli(merlin, "list").stdout) == []

        # Edit, then delete.
        page.click(".apps-card[data-id=probe] button:has-text('Edit')")
        assert page.input_value("#apps-form [name=size]") == "640x480"
        page.fill("#apps-form [name=size]", "800x600")
        page.click("#apps-form button[type=submit]")
        page.wait_for_selector("#apps-form-modal", state="hidden")
        saved = Path(merlin.home) / "data" / "apps" / "saved.json"
        assert json.loads(saved.read_text())[0]["size"] == "800x600"
        delete = page.locator(".apps-card[data-id=probe] button:has-text('Delete')")
        delete.click()
        delete.click()
        page.wait_for_selector(
            ".apps-card[data-id=probe]", state="detached", timeout=10000
        )
    finally:
        context.close()


def test_form_errors_are_shown(browser, server):
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{server}/apps")
        page.click("#apps-add")
        page.fill("#apps-form [name=name]", "broken")
        page.fill("#apps-form [name=command]", "true")
        page.fill("#apps-form [name=cwd]", "/no/such/folder")
        page.click("#apps-form button[type=submit]")
        page.wait_for_function(
            "document.getElementById('apps-form-error').textContent.includes('folder not found')"
        )
    finally:
        context.close()


def test_agent_started_app_shows_its_origin_and_can_be_saved(
    merlin, browser, server, tmp_path
):
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", "agent", "-n", "claude"],
        env=merlin.env,
        check=True,
    )
    pane = subprocess.run(
        ["tmux", "display-message", "-p", "-t", "agent:", "#{pane_id}"],
        env=merlin.env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    launch_probe(merlin, tmp_path / "p.log", "probe", env={"TMUX_PANE": pane})
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{server}/apps")
        page.wait_for_selector(".apps-card[data-id=probe] .apps-card-origin")
        assert page.inner_text(".apps-card[data-id=probe] .apps-card-origin") == (
            "started in agent:claude"
        )
        page.click(".apps-card[data-id=probe] button:has-text('Save')")
        assert page.input_value("#apps-form [name=name]") == "probe"
        assert page.input_value("#apps-form [name=command]") == str(PROBE)
        page.click("#apps-form button[type=submit]")
        page.wait_for_selector("#apps-form-modal", state="hidden")
        page.wait_for_selector(".apps-card[data-id=probe] button:has-text('Edit')")
        assert page.locator(".apps-card").count() == 1  # merged with the saved app
    finally:
        context.close()


def test_phone_launch_fits_the_device(playwright, browser, server):
    context = browser.new_context(**playwright.devices["Pixel 7"])
    page = context.new_page()
    try:
        page.goto(f"{server}/apps")
        page.tap("#apps-add")
        assert page.is_checked("#apps-form [name=size_mode][value=fit]")
        _fill_form(page, "probe", size_mode="fit", controls="gamepad")
        expected = page.evaluate("window.MerlinAppsPage.fitSize()")
        SHOTS.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOTS / "apps-mobile.png"))
        page.tap(".apps-card[data-id=probe] button:has-text('Launch')")
        page.wait_for_url("**/apps/probe/play", timeout=20000)
        wait_live(page)
        size = page.evaluate(
            "document.getElementById('player-video').videoWidth + 'x' +"
            " document.getElementById('player-video').videoHeight"
        )
        assert size == expected
    finally:
        context.close()
