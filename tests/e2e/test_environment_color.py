"""E2E tests for the environment color, in a real browser on a throwaway
Merlin whose ``config.env`` carries ``MERLIN_ENV_COLOR=blue``.

The page's favicon link and manifest link carry the color, the manifest's
icons point at the blue set and every one of them is served, the served
favicon has the blue accent, and the Settings page shows the blue swatch
selected. A click on another swatch saves it and the next load reflects it.

Run: uv run scripts.py test-e2e   (or pytest tests/e2e/test_environment_color.py)
Requires: chromium.
"""

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

MERLIN_OPTIONS = {"config": "MERLIN_ENV_COLOR=blue\n"}

BLUE = "#60a5fa"
PLATE = "#1e2035"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1100, "height": 720})
    pg = ctx.new_page()
    yield pg
    ctx.close()


def test_page_links_carry_the_color(page, server):
    page.goto(f"{server}/terminal")
    icon = page.get_attribute('link[rel="icon"][type="image/svg+xml"]', "href")
    assert icon == "/static/favicon.svg?c=blue"
    assert page.get_attribute('link[rel="manifest"]', "href") == (
        "/manifest.webmanifest?c=blue"
    )
    assert page.get_attribute('link[rel="apple-touch-icon"]', "href") == (
        "/static/icons/blue/apple-touch-icon.png"
    )
    # The theme color is still the page background: only the accent moves.
    assert page.get_attribute('meta[name="theme-color"]', "content") == "#0f1117"


def test_served_favicon_has_the_accent(page, server):
    r = page.request.get(f"{server}/static/favicon.svg?c=blue")
    assert r.status == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    svg = r.text()
    assert svg.count(BLUE) == 2
    assert f'fill="{PLATE}"' in svg


def test_manifest_points_at_the_color_set_and_every_icon_is_served(page, server):
    r = page.request.get(f"{server}/manifest.webmanifest?c=blue")
    assert r.status == 200
    body = r.json()
    assert [i["src"] for i in body["icons"]] == [
        "/static/icons/blue/icon-192.png",
        "/static/icons/blue/icon-512.png",
        "/static/icons/blue/icon-maskable-512.png",
    ]
    assert body["theme_color"] == "#0f1117"
    for icon in body["icons"]:
        png = page.request.get(f"{server}{icon['src']}")
        assert png.status == 200
        assert png.body()[:8] == b"\x89PNG\r\n\x1a\n"
    touch = page.request.get(f"{server}/static/icons/blue/apple-touch-icon.png")
    assert touch.status == 200


def test_settings_shows_the_swatch_selected_and_the_mark_colored(page, server):
    page.goto(f"{server}/settings")
    page.wait_for_selector("#env-color")
    checked = page.eval_on_selector_all(
        '.settings-swatch[aria-checked="true"]', "els => els.map(e => e.dataset.color)"
    )
    assert checked == ["blue"]
    assert page.eval_on_selector_all(".settings-swatch", "els => els.length") == 8
    border = page.eval_on_selector(
        "#sidebar-logo", "e => getComputedStyle(e).borderTopColor"
    )
    assert border == "rgb(96, 165, 250)"


def test_a_click_saves_and_the_next_load_reflects_it(page, server, merlin):
    page.goto(f"{server}/settings")
    page.click('.settings-swatch[data-color="red"]')
    page.wait_for_function(
        "document.querySelector('#toast-env-color').textContent === 'Saved'",
        timeout=5000,
    )
    assert "MERLIN_ENV_COLOR=red" in (merlin.home / "config.env").read_text()
    # In place, without a reload.
    assert page.get_attribute('link[rel="icon"][type="image/svg+xml"]', "href") == (
        "/static/favicon.svg?c=red"
    )
    # And on the next load, from the setting.
    page.goto(f"{server}/terminal")
    assert page.get_attribute('link[rel="manifest"]', "href") == (
        "/manifest.webmanifest?c=red"
    )
    manifest = page.request.get(f"{server}/manifest.webmanifest?c=red").json()
    assert manifest["icons"][0]["src"] == "/static/icons/red/icon-192.png"
    # Back to blue for the other tests of this module.
    page.goto(f"{server}/settings")
    page.click('.settings-swatch[data-color="blue"]')
    page.wait_for_function(
        "document.querySelector('#toast-env-color').textContent === 'Saved'",
        timeout=5000,
    )
    assert "MERLIN_ENV_COLOR=blue" in (merlin.home / "config.env").read_text()
