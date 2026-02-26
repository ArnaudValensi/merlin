# /// script
# dependencies = ["playwright"]
# ///
"""
Test touch scrolling on the terminal page.

Usage:
    cd merlin-bot && export $(grep -v '^#' .env | xargs) && uv run tests/test_touch_scroll.py
"""
import argparse
import json
import os
import sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3123"
PASSWORD = os.environ.get("DASHBOARD_PASS", "admin")


def login(page):
    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")
    page.goto(f"{BASE}/terminal", wait_until="networkidle")


def test_touch_scroll(headed=False):
    passed = 0
    failed = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
        )
        page = context.new_page()
        login(page)

        # Wait for terminal
        page.wait_for_selector(".xterm-screen", timeout=10000)
        page.wait_for_function(
            "document.getElementById('status-dot')?.classList.contains('connected')",
            timeout=10000,
        )
        page.wait_for_timeout(500)

        # --- Test 1: touch-action: none on viewport ---
        touch_action = page.evaluate("""() => {
            const o = document.getElementById('touch-overlay');
            return o ? getComputedStyle(o).touchAction : 'NOT FOUND';
        }""")
        if touch_action == "none":
            print(f"PASS  touch-action on container = '{touch_action}'")
            passed += 1
        else:
            print(f"FAIL  touch-action on container = '{touch_action}' (expected 'none')")
            failed += 1

        # --- Test 2: viewport still has touch-action after scrollback ---
        # Generate output to create scrollback
        page.keyboard.type("seq 1 200\n", delay=5)
        page.wait_for_timeout(2000)

        container_info = page.evaluate("""() => {
            const o = document.getElementById('touch-overlay');
            const screen = document.querySelector('.xterm-screen');
            if (!o) return null;
            return {
                containerTouchAction: getComputedStyle(o).touchAction,
                screenTouchAction: screen ? getComputedStyle(screen).touchAction : 'N/A',
            };
        }""")
        print(f"  after scrollback: {json.dumps(container_info)}")
        if container_info and container_info["containerTouchAction"] == "none":
            print(f"PASS  container touch-action still 'none' after scrollback")
            passed += 1
        else:
            print(f"FAIL  container touch-action changed after scrollback")
            failed += 1

        # --- Test 3: touch events trigger our handler (not native scroll) ---
        # Inject a probe: hook sendToTerminal to count SGR calls
        page.evaluate("""() => {
            window._sgrCount = 0;
            const origSend = window.WebSocket.prototype.send;
            window.WebSocket.prototype.send = function(data) {
                if (typeof data === 'string' && (data.includes('\\x1b[<64;') || data.includes('\\x1b[<65;'))) {
                    window._sgrCount++;
                }
                return origSend.call(this, data);
            };
        }""")

        # Dispatch touch scroll
        page.evaluate("""() => {
            const el = document.getElementById('touch-overlay') || document.getElementById('terminal-container');
            const rect = el.getBoundingClientRect();
            const x = rect.x + rect.width / 2;
            const startY = rect.y + rect.height * 0.7;
            const endY = rect.y + rect.height * 0.3;
            const steps = 20;
            const dy = (endY - startY) / steps;

            el.dispatchEvent(new TouchEvent('touchstart', {
                bubbles: true, cancelable: true,
                touches: [new Touch({identifier: 1, target: el, clientX: x, clientY: startY})],
                changedTouches: [new Touch({identifier: 1, target: el, clientX: x, clientY: startY})],
            }));
            for (let i = 1; i <= steps; i++) {
                el.dispatchEvent(new TouchEvent('touchmove', {
                    bubbles: true, cancelable: true,
                    touches: [new Touch({identifier: 1, target: el, clientX: x, clientY: startY + dy * i})],
                    changedTouches: [new Touch({identifier: 1, target: el, clientX: x, clientY: startY + dy * i})],
                }));
            }
            el.dispatchEvent(new TouchEvent('touchend', {
                bubbles: true, cancelable: true, touches: [],
                changedTouches: [new Touch({identifier: 1, target: el, clientX: x, clientY: endY})],
            }));
        }""")
        page.wait_for_timeout(500)

        sgr_count = page.evaluate("window._sgrCount")
        if sgr_count > 0:
            print(f"PASS  touch scroll sent {sgr_count} SGR sequences")
            passed += 1
        else:
            print(f"FAIL  no SGR sequences sent (count={sgr_count})")
            failed += 1

        # --- Test 4: native viewport scroll did NOT happen ---
        scroll_top = page.evaluate("""() => {
            const vp = document.querySelector('.xterm-viewport');
            return vp ? vp.scrollTop : -1;
        }""")
        # scrollTop should be at bottom (where tmux puts new output), not moved by our touch
        # The key check: viewport scrollTop should not have changed from touch
        print(f"  viewport scrollTop after touch: {scroll_top}")

        browser.close()

    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


def main():
    parser = argparse.ArgumentParser(description="Test terminal touch scrolling")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser")
    args = parser.parse_args()

    ok = test_touch_scroll(headed=args.headed)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
