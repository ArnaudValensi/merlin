"""E2E tests for native link behavior in the File Browser listing.

These tests drive Firefox against an isolated Merlin server and prove that
ordinary clicks keep SPA navigation while a real middle-click opens a deep link
in a second tab. They also cover URL-sensitive filenames and selection mode.

Run with:
    uv run --with pytest --with playwright pytest tests/e2e/test_file_navigation.py -v

Requires:
    uv run --with playwright playwright install firefox
"""

import os
import signal
import socket
import subprocess
import time

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright


def _find_free_port():
    with socket.socket() as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def navigation_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("file-navigation")
    (root / "ordinary.txt").write_text("ordinary target\n")
    (root / "folder").mkdir()
    special_name = "notes #50% ü?.txt"
    (root / special_name).write_text("reserved characters target\n")
    return root, special_name


@pytest.fixture(scope="module")
def server(navigation_files, tmp_path_factory):
    """Start a Merlin server with isolated state on a random port."""
    port = _find_free_port()
    merlin_home = tmp_path_factory.mktemp("file-navigation-home")
    (merlin_home / "config.env").write_text("DASHBOARD_PASS=\n")
    env = os.environ.copy()
    env["DASHBOARD_PASS"] = ""
    env["MERLIN_SAAS_TOKEN"] = ""
    env["DISCORD_BOT_TOKEN"] = ""
    env["DISCORD_CHANNEL_IDS"] = ""
    env["MERLIN_HOME"] = str(merlin_home)
    env["MERLIN_DEV"] = "1"

    merlin_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    proc = subprocess.Popen(
        ["uv", "run", "main.py", "--port", str(port)],
        cwd=merlin_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    url = f"http://localhost:{port}"
    for _ in range(30):
        try:
            import urllib.request

            urllib.request.urlopen(f"{url}/api/files/browse?path=/tmp", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("Server failed to start")

    yield url

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def browser_context(server):
    with sync_playwright() as playwright:
        browser = playwright.firefox.launch(headless=True)
        context = browser.new_context(viewport={"width": 1200, "height": 800})
        yield context, server
        context.close()
        browser.close()


def _open_listing(context, url, directory):
    page = context.new_page()
    page.goto(f"{url}/files{directory}", wait_until="networkidle")
    page.get_by_role("link", name="Open file ordinary.txt", exact=True).wait_for(
        timeout=15000
    )
    return page


class TestNativeFileLinks:
    def test_rows_are_named_native_links(self, browser_context, navigation_files):
        context, url = browser_context
        directory, special_name = navigation_files
        page = _open_listing(context, url, directory)

        ordinary = page.get_by_role("link", name="Open file ordinary.txt", exact=True)
        folder = page.get_by_role("link", name="Open folder folder", exact=True)
        special = page.get_by_role("link", name=f"Open file {special_name}", exact=True)

        assert ordinary.evaluate("element => element.tagName") == "A"
        assert folder.evaluate("element => element.tagName") == "A"
        special_href = special.get_attribute("href")
        assert "%20" in special_href
        assert "%23" in special_href
        assert "%25" in special_href
        assert "%3F" in special_href
        assert "%C3%BC" in special_href

        page.close()

    def test_primary_click_keeps_same_tab_navigation(
        self, browser_context, navigation_files
    ):
        context, url = browser_context
        directory, _ = navigation_files
        page = _open_listing(context, url, directory)

        page.get_by_role("link", name="Open file ordinary.txt", exact=True).click()
        page.locator("#file-view").wait_for(state="visible")

        assert page.url.endswith("/ordinary.txt")
        assert "ordinary target" in page.locator("#file-content").inner_text()
        assert len(context.pages) == 1

        page.close()

    def test_keyboard_activation_uses_the_row_link(
        self, browser_context, navigation_files
    ):
        context, url = browser_context
        directory, _ = navigation_files
        page = _open_listing(context, url, directory)
        link = page.get_by_role("link", name="Open file ordinary.txt", exact=True)

        page.keyboard.press("Tab")
        link.focus()
        assert link.evaluate("element => document.activeElement === element")
        outline = link.evaluate("element => getComputedStyle(element).outline")
        assert outline == "rgb(74, 158, 255) solid 2px"
        page.keyboard.press("Enter")
        page.locator("#file-view").wait_for(state="visible")

        assert page.url.endswith("/ordinary.txt")
        assert "ordinary target" in page.locator("#file-content").inner_text()

        page.close()

    def test_middle_click_opens_encoded_path_in_new_tab(
        self, browser_context, navigation_files
    ):
        context, url = browser_context
        directory, special_name = navigation_files
        page = _open_listing(context, url, directory)
        listing_url = page.url
        special = page.get_by_role("link", name=f"Open file {special_name}", exact=True)

        with context.expect_page() as page_info:
            special.click(button="middle")
        opened = page_info.value
        opened.wait_for_load_state("networkidle")
        opened.locator("#file-view").wait_for(state="visible")

        assert page.url == listing_url
        assert page.locator("#dir-view").is_visible()
        assert (
            "reserved characters target" in opened.locator("#file-content").inner_text()
        )
        assert "%23" in opened.url
        assert "%25" in opened.url
        assert "%3F" in opened.url

        opened.close()
        page.close()

    def test_every_modifier_click_remains_uncancelled(
        self, browser_context, navigation_files
    ):
        context, url = browser_context
        directory, _ = navigation_files
        page = _open_listing(context, url, directory)
        link = page.get_by_role("link", name="Open file ordinary.txt", exact=True)

        prevented = link.evaluate(
            """element => {
                const modifiers = ['ctrlKey', 'metaKey', 'shiftKey', 'altKey'];
                return modifiers.map(modifier => {
                    let wasPrevented = null;
                    element.addEventListener('click', event => {
                        wasPrevented = event.defaultPrevented;
                        event.preventDefault();
                    }, {once: true});
                    element.dispatchEvent(new MouseEvent('click', {
                        bubbles: true,
                        cancelable: true,
                        button: 0,
                        [modifier]: true,
                    }));
                    return wasPrevented;
                });
            }"""
        )

        assert prevented == [False, False, False, False]
        page.close()

    def test_control_click_is_left_to_the_browser(
        self, browser_context, navigation_files
    ):
        context, url = browser_context
        directory, _ = navigation_files
        page = _open_listing(context, url, directory)
        listing_url = page.url

        with context.expect_page() as page_info:
            page.get_by_role("link", name="Open file ordinary.txt", exact=True).click(
                modifiers=["Control"]
            )
        opened = page_info.value
        opened.wait_for_load_state("networkidle")
        opened.locator("#file-view").wait_for(state="visible")

        assert page.url == listing_url
        assert "ordinary target" in opened.locator("#file-content").inner_text()

        opened.close()
        page.close()

    def test_selection_mode_removes_links_and_selects_rows(
        self, browser_context, navigation_files
    ):
        context, url = browser_context
        directory, _ = navigation_files
        page = _open_listing(context, url, directory)

        page.get_by_role("button", name="Select files", exact=True).click()

        assert page.locator(".dir-entry-link").count() == 0
        page.locator(".dir-entry", has_text="ordinary.txt").click()
        checkbox = page.get_by_role("checkbox", name="Select ordinary.txt", exact=True)
        assert checkbox.get_attribute("aria-checked") == "true"
        assert page.url.endswith(str(directory))

        page.close()

    def test_inline_rename_removes_the_link_overlay(
        self, browser_context, navigation_files
    ):
        context, url = browser_context
        directory, _ = navigation_files
        page = _open_listing(context, url, directory)

        page.get_by_role("button", name="Select files", exact=True).click()
        page.locator(".dir-entry", has_text="ordinary.txt").click()
        page.get_by_role("button", name="Rename selected item", exact=True).click()

        rename_input = page.get_by_role("textbox", name="Rename to", exact=True)
        rename_input.wait_for(state="visible")
        edited_row = rename_input.locator(
            "xpath=ancestor::*[contains(@class, 'dir-entry')]"
        )
        assert edited_row.locator(".dir-entry-link").count() == 0

        rename_input.press("Escape")
        page.get_by_role("link", name="Open file ordinary.txt", exact=True).wait_for()
        page.close()
