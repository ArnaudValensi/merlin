"""E2E tests for the sandboxed HTML preview in Files.

Runs with auth on, so the isolation claims are real: the preview route must
work without the session cookie, and a script inside the page must not reach
the dashboard API, its DOM, or its cookie.

Run with: uv run scripts.py test-e2e
"""

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

TEST_PASSWORD = "html-preview-test"
MERLIN_OPTIONS = {"password": TEST_PASSWORD}

# Each probe writes its verdict into an element the test reads back.
INDEX_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<link rel="stylesheet" href="css/style.css">
<script type="module" src="./js/main.js"></script>
</head><body>
<h1 id="title">Hello from a page</h1>
<p>origin <span id="origin"></span></p>
<p>storage <span id="ls"></span></p>
<p>cookie <span id="cookie"></span></p>
<p>parent <span id="parent"></span></p>
<p>api <span id="api"></span></p>
<p>hidden <span id="hidden"></span></p>
<p>data <span id="data"></span></p>
<script>
const put = (id, v) => { document.getElementById(id).textContent = v; };
put('origin', self.origin);
try { localStorage.setItem('k', 'v'); put('ls', localStorage.getItem('k')); }
catch (e) { put('ls', 'threw'); }
try { document.cookie = 'a=1'; put('cookie', document.cookie); }
catch (e) { put('cookie', 'threw'); }
try { put('parent', window.parent.document.title ? 'reachable' : 'reachable'); }
catch (e) { put('parent', 'blocked'); }
fetch('/api/files/browse?path=/', { credentials: 'include' })
  .then(r => put('api', r.ok ? 'reachable' : 'status ' + r.status))
  .catch(() => put('api', 'blocked'));
fetch('.env').then(r => put('hidden', String(r.status))).catch(() => put('hidden', 'error'));
fetch('data/info.json').then(r => r.json()).then(j => put('data', j.answer));
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("htmlsite")
    (root / "css").mkdir()
    (root / "js").mkdir()
    (root / "data").mkdir()
    (root / "index.html").write_text(INDEX_HTML)
    (root / "css" / "style.css").write_text("h1 { color: rgb(255, 0, 0); }")
    (root / "js" / "main.js").write_text(
        "import { mark } from './lib.js';\ndocument.body.dataset.module = mark;\n"
    )
    (root / "js" / "lib.js").write_text("export const mark = 'ok';\n")
    (root / "data" / "info.json").write_text('{"answer": "42"}')
    (root / ".env").write_text("SECRET=1\n")
    (root / "notes.txt").write_text("plain text neighbour\n")
    return root


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture()
def page(browser, server):
    ctx = browser.new_context(viewport={"width": 1200, "height": 800})
    pg = ctx.new_page()
    pg.goto(f"{server}/login")
    pg.fill('input[name="password"]', TEST_PASSWORD)
    pg.click('button[type="submit"]')
    pg.wait_for_load_state("networkidle")
    yield pg
    ctx.close()


def _open(page, server, site):
    page.goto(f"{server}/files{site / 'index.html'}")
    page.wait_for_selector("iframe.html-preview", timeout=10000)
    frame = page.frame_locator("iframe.html-preview")
    frame.locator("#title").wait_for(timeout=10000)
    return frame


def test_page_runs_with_its_assets(page, server, site):
    frame = _open(page, server, site)
    assert frame.locator("#title").text_content() == "Hello from a page"
    # Relative stylesheet applied
    color = frame.locator("#title").evaluate("el => getComputedStyle(el).color")
    assert color == "rgb(255, 0, 0)"
    # ES module graph (main.js imports lib.js) loaded under the same token
    frame.locator("body[data-module='ok']").wait_for(timeout=5000)
    # fetch() of a relative JSON file
    frame.locator("#data:text('42')").wait_for(timeout=5000)


def test_page_is_isolated_from_the_dashboard(page, server, site):
    frame = _open(page, server, site)
    assert frame.locator("#origin").text_content() == "null"
    assert frame.locator("#parent").text_content() == "blocked"
    frame.locator("#api:not(:empty)").wait_for(timeout=5000)
    api = frame.locator("#api").text_content()
    assert api != "reachable", api
    # Hidden files next to the page are refused
    frame.locator("#hidden:text('404')").wait_for(timeout=5000)


def test_storage_shim_keeps_apps_running(page, server, site):
    frame = _open(page, server, site)
    assert frame.locator("#ls").text_content() == "v"
    assert frame.locator("#cookie").text_content() == "a=1"
    # It is the shim answering, not real storage: the opaque origin really
    # does refuse the browser's own Storage.
    is_real = frame.locator("body").evaluate("() => localStorage instanceof Storage")
    assert is_real is False


def test_source_toggle_round_trip(page, server, site):
    _open(page, server, site)
    toggle = page.locator("#md-toggle")
    assert toggle.text_content() == "Source"
    toggle.click()
    page.wait_for_selector(".file-table", timeout=5000)
    assert page.query_selector("iframe.html-preview") is None
    assert "Hello from a page" in page.locator("#file-content").text_content()
    assert toggle.text_content() == "Rendered"
    toggle.click()
    page.frame_locator("iframe.html-preview").locator("#title").wait_for(timeout=10000)
    assert toggle.text_content() == "Source"


def test_new_tab_is_sandboxed_and_needs_no_cookie(page, browser, server, site):
    _open(page, server, site)
    link = page.locator("#open-tab-link")
    assert link.is_visible()
    href = link.get_attribute("href")
    assert href.startswith("/files-view/")
    # A fresh context has no session cookie: the token alone must serve it.
    ctx = browser.new_context()
    try:
        tab = ctx.new_page()
        tab.goto(f"{server}{href}")
        tab.wait_for_selector("body[data-module='ok']", timeout=10000)
        assert tab.evaluate("() => self.origin") == "null"
        assert tab.locator("#ls").text_content() == "v"
    finally:
        ctx.close()


def test_non_html_hides_html_controls(page, server, site):
    _open(page, server, site)
    page.goto(f"{server}/files{site / 'notes.txt'}")
    page.wait_for_selector(".file-table", timeout=5000)
    assert not page.locator("#open-tab-link").is_visible()
    assert not page.locator("#md-toggle").is_visible()
