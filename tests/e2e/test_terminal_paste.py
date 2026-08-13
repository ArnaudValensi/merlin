"""E2E tests for the web terminal's paste paths, in a real browser.

Every paste entry point (button, Ctrl+V, right-click) converges on
handlePaste(), which walks the ladder in terminal/templates/clipboard-core.js.
tests/js/clipboard-core.test.js checks that ladder in isolation; this file
checks it is actually wired to the terminal — that a paste reaches the shell
exactly once and that a failure reports the right thing.

Two cases substitute a fake navigator.clipboard to reproduce mobile behaviour
that no desktop browser will produce on demand:

  * iOS Brave — read() resolves (the user tapped the native paste button) but
    hands back items with no usable flavour. The terminal must recover through
    readText() instead of reporting "Clipboard blocked" over a clipboard that
    plainly has text in it.
  * a dismissed paste prompt — read() rejects with NotAllowedError. The
    terminal must NOT call readText(), which would only raise a second prompt
    for a refusal the user already made.

Run: uv run scripts.py test-e2e   (or pytest tests/e2e/test_terminal_paste.py)
Requires: chromium (clipboard permissions cannot be granted in Firefox) + tmux.
"""

import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="web terminal needs tmux"
)


def _find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Merlin without auth on a random port, on its own tmux server."""
    port = _find_free_port()
    env = os.environ.copy()
    env["DASHBOARD_PASS"] = ""
    env["MERLIN_SAAS_TOKEN"] = ""
    env["DISCORD_BOT_TOKEN"] = ""
    env["DISCORD_CHANNEL_IDS"] = ""
    env.pop("MERLIN_HOME", None)

    # Never touch the tmux server the developer is sitting in: an inherited
    # $TMUX makes the spawned `tmux new-session` refuse to nest, and a shared
    # socket would attach the test to real windows. The socket path is a unix
    # socket, so it has to stay well short of ~108 chars.
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = str(tmp_path_factory.mktemp("tmux"))

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
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{url}/terminal", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("Server failed to start")

    yield url

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def terminal(server):
    """A live terminal page with clipboard permissions granted."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1100, "height": 720},
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = ctx.new_page()
        page.goto(f"{server}/terminal")
        page.wait_for_selector(".xterm-screen", timeout=30000)
        page.wait_for_timeout(3000)  # let tmux draw its first prompt
        yield page
        ctx.close()
        browser.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# navigator.clipboard as iOS Brave presents it in the bug: read() succeeds but
# offers only a flavour the terminal refuses to paste into a shell.
FAKE_UNUSABLE_READ = """(text) => {
    window.__readTextCalls = 0;
    Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: {
            read: async () => [{
                types: ['text/html'],
                getType: async () => new Blob(['<b>x</b>'], { type: 'text/html' }),
            }],
            readText: async () => { window.__readTextCalls++; return text; },
        },
    });
}"""

# navigator.clipboard after the user dismisses the native paste prompt.
FAKE_DENIED_READ = """() => {
    window.__readTextCalls = 0;
    Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: {
            read: async () => {
                throw Object.assign(new Error('denied'), { name: 'NotAllowedError' });
            },
            readText: async () => { window.__readTextCalls++; return 'must not be used'; },
        },
    });
}"""

SCREEN_TEXT = (
    "() => Array.from(document.querySelectorAll('.xterm-rows > div'))"
    ".map(r => r.textContent).join('\\n')"
)


def _reset_line(page):
    """Clear the shell line so a leftover paste cannot be miscounted."""
    page.click(".xterm-screen")
    page.keyboard.press("Control+u")
    page.wait_for_timeout(400)


def _paste_count(page, marker, action):
    """Run `action` and report how many more times `marker` is on screen."""
    _reset_line(page)
    before = page.evaluate(SCREEN_TEXT).count(marker)
    action()
    page.wait_for_timeout(1200)
    return page.evaluate(SCREEN_TEXT).count(marker) - before


def _status(page):
    return " ".join((page.text_content("#terminal-status") or "").split())


# ---------------------------------------------------------------------------
# Every entry point pastes, once
# ---------------------------------------------------------------------------


def test_paste_button_pastes_once(terminal):
    terminal.evaluate("t => navigator.clipboard.writeText(t)", "PASTE_BUTTON_MARK")
    assert (
        _paste_count(
            terminal, "PASTE_BUTTON_MARK", lambda: terminal.click("#paste-btn")
        )
        == 1
    )
    assert "Pasted!" in _status(terminal)


def test_ctrl_v_pastes_once(terminal):
    terminal.evaluate("t => navigator.clipboard.writeText(t)", "CTRL_V_MARK")

    def press():
        terminal.click(".xterm-screen")
        terminal.keyboard.press("Control+v")

    assert _paste_count(terminal, "CTRL_V_MARK", press) == 1


def test_right_click_pastes_once(terminal):
    terminal.evaluate("t => navigator.clipboard.writeText(t)", "RIGHT_CLICK_MARK")
    assert (
        _paste_count(
            terminal,
            "RIGHT_CLICK_MARK",
            lambda: terminal.click(".xterm-screen", button="right"),
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Mobile behaviour a desktop browser will not produce on its own
# ---------------------------------------------------------------------------


def test_unusable_read_falls_back_to_read_text(terminal):
    """The iOS Brave bug: read() succeeds but yields nothing we can paste."""
    terminal.evaluate(FAKE_UNUSABLE_READ, "IOS_BRAVE_MARK")
    assert (
        _paste_count(terminal, "IOS_BRAVE_MARK", lambda: terminal.click("#paste-btn"))
        == 1
    )
    assert terminal.evaluate("() => window.__readTextCalls") == 1
    assert "Clipboard blocked" not in _status(terminal)
    terminal.reload()
    terminal.wait_for_selector(".xterm-screen", timeout=30000)
    terminal.wait_for_timeout(2500)


def test_denied_read_does_not_prompt_twice(terminal):
    """A dismissed paste prompt must not be raised again through readText()."""
    _reset_line(terminal)
    terminal.evaluate(FAKE_DENIED_READ)
    terminal.click("#paste-btn")
    terminal.wait_for_timeout(1200)

    assert terminal.evaluate("() => window.__readTextCalls") == 0
    assert "Clipboard blocked" in _status(terminal)
    # The reason is readable from the page, so a phone with no console can
    # still say which of the three failures it hit.
    assert (
        terminal.evaluate("() => window.MerlinTerminal.lastPasteResult()")["reason"]
        == "blocked"
    )
    terminal.reload()
    terminal.wait_for_selector(".xterm-screen", timeout=30000)
    terminal.wait_for_timeout(2500)


# ---------------------------------------------------------------------------
# The diagnostic page must run the same ladder as the terminal
# ---------------------------------------------------------------------------


def test_diagnostic_page_traces_the_real_ladder(terminal, server):
    page = terminal.context.new_page()
    page.goto(f"{server}/terminal/clipboard-test")
    page.wait_for_selector("#btn-trace")
    page.evaluate("t => navigator.clipboard.writeText(t)", "TRACE_MARK")
    page.click("#btn-trace")
    page.wait_for_timeout(1200)

    trace = page.text_content("#trace-log")
    assert "TEXT would be pasted" in trace
    assert "TRACE_MARK" in trace
    page.close()
