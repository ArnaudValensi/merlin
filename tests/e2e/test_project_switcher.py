"""E2E tests for the environment switcher in the sidebar (SaaS mode).

Uses Playwright to test the sidebar rendering and behavior in both
standalone and SaaS modes.

Run with: uv run --with pytest --with playwright pytest tests/test_project_switcher.py -v
Requires: uv run --with playwright playwright install firefox
"""

import json
import os
import signal
import socket
import subprocess
import time

import pytest

# Skip all tests if playwright is not installed
pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

MOCK_PROJECTS = [
    {
        "id": 1,
        "name": "merlin-saas",
        "slug": "merlin-saas",
        "url": "https://merlin-saas.merlincloud.dev",
        "is_online": True,
        "is_current": True,
    },
    {
        "id": 2,
        "name": "my-app",
        "slug": "my-app",
        "url": "https://my-app.merlincloud.dev",
        "is_online": True,
        "is_current": False,
    },
    {
        "id": 3,
        "name": "side-project",
        "slug": "side-project",
        "url": "https://side-project.merlincloud.dev",
        "is_online": False,
        "is_current": False,
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


TEST_PASSWORD = "testpass123"


def _start_server(saas_token=""):
    """Start a Merlin server instance. Returns (url, process)."""
    port = _find_free_port()
    env = os.environ.copy()
    env["DASHBOARD_PASS"] = TEST_PASSWORD
    env["MERLIN_SAAS_TOKEN"] = saas_token
    env["MERLIN_SAAS_API"] = "https://merlincloud.dev"
    env["DISCORD_BOT_TOKEN"] = ""
    env["DISCORD_CHANNEL_IDS"] = ""
    # Don't inherit MERLIN_HOME from test isolation — the subprocess needs
    # the real ~/.merlin/ to find config.env, extensions, etc.
    env.pop("MERLIN_HOME", None)

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

    return url, proc


@pytest.fixture(scope="module")
def standalone_server():
    """Start the Merlin server in standalone mode (no MERLIN_SAAS_TOKEN)."""
    url, proc = _start_server(saas_token="")
    yield url
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def saas_server():
    """Start the Merlin server in SaaS mode (MERLIN_SAAS_TOKEN set)."""
    url, proc = _start_server(saas_token="mrl_test_fake_token_1234567890")
    yield url
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def playwright_instance():
    """Provide a Playwright instance for the test module."""
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright_instance):
    """Launch a browser for the test module."""
    b = playwright_instance.firefox.launch(headless=True)
    yield b
    b.close()


def _login(page, server_url):
    """Login to the Merlin dashboard via the login form."""
    page.goto(f"{server_url}/login")
    page.fill('input[name="password"]', TEST_PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


# ---------------------------------------------------------------------------
# Standalone mode tests
# ---------------------------------------------------------------------------


class TestStandaloneMode:
    """Environment switcher should be invisible in standalone mode."""

    def test_switcher_hidden_standalone_mode(self, browser, standalone_server):
        """No MERLIN_SAAS_TOKEN → no environment switcher in sidebar."""
        ctx = browser.new_context(viewport={"width": 1200, "height": 800})
        page = ctx.new_page()
        _login(page, standalone_server)
        page.goto(f"{standalone_server}/files")
        page.wait_for_load_state("networkidle")

        switcher = page.query_selector("#env-switcher")
        assert switcher is None, (
            "Environment switcher should not exist in standalone mode"
        )

        ctx.close()

    def test_no_api_call_standalone(self, browser, standalone_server):
        """Standalone mode should not make any API call to the portal."""
        ctx = browser.new_context(viewport={"width": 1200, "height": 800})
        page = ctx.new_page()
        _login(page, standalone_server)

        api_calls = []
        page.on(
            "request",
            lambda req: api_calls.append(req.url) if "merlincloud" in req.url else None,
        )

        page.goto(f"{standalone_server}/files")
        page.wait_for_load_state("networkidle")

        assert len(api_calls) == 0, f"No portal API calls expected, got: {api_calls}"

        ctx.close()


# ---------------------------------------------------------------------------
# SaaS mode tests
# ---------------------------------------------------------------------------


class TestSaaSMode:
    """Environment switcher should be visible and functional in SaaS mode."""

    def _setup_page_with_mock(self, browser, saas_server, environments=None):
        """Create a page that intercepts the portal API call and returns mock data.

        The first environment's URL is set to the test server origin so that
        the JS is_current detection (URL origin matching) works correctly.
        """
        if environments is None:
            # Deep copy and set first environment URL to match the test server
            environments = json.loads(json.dumps(MOCK_PROJECTS))
            environments[0]["url"] = saas_server  # merlin-saas → current

        ctx = browser.new_context(viewport={"width": 1200, "height": 800})
        page = ctx.new_page()

        # Login first (SaaS mode requires auth)
        _login(page, saas_server)

        # Intercept the portal API call
        def handle_route(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(environments),
            )

        page.route("**/api/environments", handle_route)
        page.goto(f"{saas_server}/files")
        page.wait_for_load_state("networkidle")
        # Give JS time to fetch and render
        page.wait_for_timeout(500)

        return ctx, page

    def test_switcher_visible_saas_mode(self, browser, saas_server):
        """MERLIN_SAAS_TOKEN set → environment switcher container exists."""
        ctx, page = self._setup_page_with_mock(browser, saas_server)

        switcher = page.query_selector("#env-switcher")
        assert switcher is not None, "Environment switcher should exist in SaaS mode"
        assert switcher.is_visible()

        ctx.close()

    def test_environments_rendered_in_list(self, browser, saas_server):
        """Mock API returns 3 environments → 3 environment items in the DOM."""
        ctx, page = self._setup_page_with_mock(browser, saas_server)

        items = page.query_selector_all(".env-item")
        assert len(items) == 3

        ctx.close()

    def test_current_environment_highlighted(self, browser, saas_server):
        """Environment with is_current: true has the 'current' class."""
        ctx, page = self._setup_page_with_mock(browser, saas_server)

        current = page.query_selector(".env-item.current")
        assert current is not None, "Current environment should have .current class"

        name = current.query_selector(".env-name")
        assert name is not None
        assert name.inner_text() == "merlin-saas"

        ctx.close()

    def test_online_status_dot(self, browser, saas_server):
        """Online environments have a dot with the 'online' class."""
        ctx, page = self._setup_page_with_mock(browser, saas_server)

        online_dots = page.query_selector_all(".env-dot.online")
        # merlin-saas and my-app are online
        assert len(online_dots) == 2

        ctx.close()

    def test_offline_status_dot(self, browser, saas_server):
        """Offline environments have a dot with the 'offline' class."""
        ctx, page = self._setup_page_with_mock(browser, saas_server)

        offline_dots = page.query_selector_all(".env-dot.offline")
        # side-project is offline
        assert len(offline_dots) == 1

        ctx.close()

    def test_section_label_present(self, browser, saas_server):
        """'Environments' label is rendered above the list."""
        ctx, page = self._setup_page_with_mock(browser, saas_server)

        label = page.query_selector(".env-switcher-label")
        assert label is not None
        assert label.inner_text().strip().lower() == "environments"

        ctx.close()

    def test_environment_name_truncation(self, browser, saas_server):
        """Long environment name is truncated (not overflowing sidebar)."""
        long_name_environments = [
            {
                "id": 1,
                "name": "a-very-long-project-name-that-should-be-truncated-with-ellipsis",
                "slug": "long",
                "url": "https://long.merlincloud.dev",
                "is_online": True,
                "is_current": True,
            }
        ]
        ctx, page = self._setup_page_with_mock(
            browser, saas_server, long_name_environments
        )

        name_el = page.query_selector(".env-name")
        assert name_el is not None

        # The name element should have overflow:hidden and text-overflow:ellipsis
        overflow = name_el.evaluate("el => getComputedStyle(el).overflow")
        assert overflow == "hidden"

        ctx.close()

    def test_click_environment_navigates(self, browser, saas_server):
        """Click on a non-current environment is an <a> with href."""
        ctx, page = self._setup_page_with_mock(browser, saas_server)

        # Find a non-current environment item
        items = page.query_selector_all("a.env-item:not(.current)")
        assert len(items) > 0

        # Check it has an href attribute
        href = items[0].get_attribute("href")
        assert href is not None
        assert href.startswith("http")

        ctx.close()

    def test_click_current_environment_noop(self, browser, saas_server):
        """Current environment item is a <span>, not a link."""
        ctx, page = self._setup_page_with_mock(browser, saas_server)

        current = page.query_selector(".env-item.current")
        assert current is not None

        # Current environment should be a <span>, not an <a>
        tag = current.evaluate("el => el.tagName.toLowerCase()")
        assert tag == "span", "Current environment should be a <span>, not a link"

        ctx.close()

    def test_api_error_hides_switcher(self, browser, saas_server):
        """API returns 500 → environment section hidden."""
        ctx = browser.new_context(viewport={"width": 1200, "height": 800})
        page = ctx.new_page()
        _login(page, saas_server)

        def handle_route(route):
            route.fulfill(status=500, body="Internal Server Error")

        page.route("**/api/environments", handle_route)
        page.goto(f"{saas_server}/files")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)

        switcher = page.query_selector("#env-switcher")
        if switcher:
            assert not switcher.is_visible(), "Switcher should be hidden on API error"

        ctx.close()

    def test_switcher_below_nav_above_footer(self, browser, saas_server):
        """Environment switcher is below nav items, above footer, all in scrollable body."""
        ctx, page = self._setup_page_with_mock(browser, saas_server)

        nav = page.query_selector(".sidebar-nav")
        switcher = page.query_selector("#env-switcher")
        footer = page.query_selector(".sidebar-footer")

        assert nav is not None
        assert switcher is not None
        assert footer is not None

        nav_box = nav.bounding_box()
        switcher_box = switcher.bounding_box()
        footer_box = footer.bounding_box()

        assert nav_box is not None
        assert switcher_box is not None
        assert footer_box is not None

        # Switcher should be below nav and above footer
        assert switcher_box["y"] > nav_box["y"], "Switcher should be below nav"
        assert switcher_box["y"] < footer_box["y"], "Switcher should be above footer"

        # Switcher should be inside the scrollable body
        parent = switcher.evaluate(
            "el => el.parentElement.classList.contains('sidebar-body')"
        )
        assert parent, "Switcher should be inside .sidebar-body"

        ctx.close()

    def test_mobile_touch_target_size(self, browser, saas_server):
        """Environment items have at least 44px height at mobile viewport."""
        ctx = browser.new_context(viewport={"width": 375, "height": 812})
        page = ctx.new_page()
        _login(page, saas_server)

        def handle_route(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(MOCK_PROJECTS),
            )

        page.route("**/api/environments", handle_route)
        page.goto(f"{saas_server}/files")
        page.wait_for_load_state("networkidle")

        # Open the mobile sidebar
        hamburger = page.query_selector(".hamburger")
        if hamburger:
            hamburger.click()
            page.wait_for_timeout(500)

        items = page.query_selector_all(".env-item")
        for item in items:
            box = item.bounding_box()
            if box:
                assert box["height"] >= 44, f"Touch target too small: {box['height']}px"

        ctx.close()
