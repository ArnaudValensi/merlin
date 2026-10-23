"""E2E test for the web terminal following the software keyboard on a phone.

No desktop browser opens a software keyboard, so the page gets a fake
window.visualViewport whose height the test drives. The case covered is the
backgrounded app: iOS closes the keyboard while the page is suspended and the
closing resize never arrives. Back in front, the page must measure again and
drop the keyboard layout instead of keeping a dead band at the bottom.

Run: uv run scripts.py test-e2e   (or pytest tests/e2e/test_terminal_keyboard.py)
Requires: chromium + tmux.
"""

import shutil

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="web terminal needs tmux"
)

# A visualViewport the test controls, plus a switch for document.hidden.
FAKE_VIEWPORT = """
(() => {
    const listeners = {};
    const vv = {
        height: window.innerHeight, width: window.innerWidth,
        scale: 1, offsetTop: 0, offsetLeft: 0,
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        removeEventListener() {},
    };
    Object.defineProperty(window, 'visualViewport', { get: () => vv });
    let hidden = false;
    Object.defineProperty(document, 'hidden', { get: () => hidden });
    Object.defineProperty(document, 'visibilityState',
        { get: () => hidden ? 'hidden' : 'visible' });
    window.__kb = {
        resize(height) {
            vv.height = height;
            (listeners.resize || []).forEach(fn => fn());
        },
        setHeightSilently(height) { vv.height = height; },
        setHidden(value) {
            hidden = value;
            document.dispatchEvent(new Event('visibilitychange'));
        },
    };
})();
"""


@pytest.fixture(scope="module")
def phone(server):
    """A live terminal page on a touch device with a scriptable keyboard."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
        )
        ctx.add_init_script(FAKE_VIEWPORT)
        page = ctx.new_page()
        page.goto(f"{server}/terminal")
        page.wait_for_selector(".xterm-screen", timeout=30000)
        yield page
        ctx.close()
        browser.close()


def keyboard_layout(page):
    return page.evaluate(
        """() => ({
            open: document.documentElement.classList.contains('keyboard-open'),
            height: document.querySelector('.main').style.height,
        })"""
    )


def test_keyboard_lost_in_background_is_dropped_on_return(phone):
    phone.evaluate("() => document.querySelector('.xterm-helper-textarea').focus()")
    phone.evaluate("() => window.__kb.resize(500)")
    assert keyboard_layout(phone) == {"open": True, "height": "500px"}

    phone.evaluate("() => window.__kb.setHidden(true)")
    focused = phone.evaluate(
        "() => document.activeElement === document.querySelector('.xterm-helper-textarea')"
    )
    assert not focused, "the terminal kept its focus into the background"

    # The keyboard closes while suspended: no resize reaches the page.
    phone.evaluate("() => window.__kb.setHeightSilently(window.innerHeight)")
    phone.evaluate("() => window.__kb.setHidden(false)")
    assert keyboard_layout(phone) == {"open": False, "height": ""}
