"""E2E tests for the notifications feature, in a real browser over
http://localhost on a throwaway Merlin with its own tmux server.

A transition is injected by stamping @agent_state on a second tmux window (the
same thing the agent hooks do). The Notification constructor and the Badging
API are replaced by stubs that record their calls, since a headless browser
shows nothing and grants nothing.

Run: uv run scripts.py test-e2e   (or pytest tests/e2e/test_notifications.py)
Requires: chromium + tmux.
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
    shutil.which("tmux") is None, reason="the watcher needs tmux"
)

SESSION = "merlin-dev"  # the session the web terminal creates on first attach


def _find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def tmux_env(tmp_path_factory):
    env = os.environ.copy()
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = str(tmp_path_factory.mktemp("tmux"))
    return env


@pytest.fixture(scope="module")
def server(tmux_env, tmp_path_factory):
    """Merlin without auth on a random port, on its own tmux server and its
    own home, so nothing touches ~/.merlin (config, jobs, logs, server state)."""
    port = _find_free_port()
    home = tmp_path_factory.mktemp("home")
    (home / "config.env").write_text("DASHBOARD_PASS=\n")
    env = dict(tmux_env)
    env["DASHBOARD_PASS"] = ""
    env["MERLIN_SAAS_TOKEN"] = ""
    env["DISCORD_BOT_TOKEN"] = ""
    env["DISCORD_CHANNEL_IDS"] = ""
    env["MERLIN_HOME"] = str(home)
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
    subprocess.run(["tmux", "kill-server"], env=tmux_env, capture_output=True)


def tmux(env, *args):
    out = subprocess.run(
        ["tmux", *args], env=env, capture_output=True, text=True, timeout=5
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


# Stubs installed before any page script runs. Notification records each
# construction, the Badging API records each value, and the preference is on.
STUBS = """
localStorage.setItem('notify-in-browser', '1');
window.__notifs = [];
window.__badges = [];
class FakeNotification {
  constructor(title, options) {
    this.title = title;
    this.options = options || {};
    window.__notifs.push({ title: title, options: this.options });
  }
  close() {}
  static requestPermission() { return Promise.resolve('granted'); }
}
FakeNotification.permission = 'granted';
Object.defineProperty(window, 'Notification', { value: FakeNotification, configurable: true });
Object.defineProperty(navigator, 'setAppBadge', {
  value: function (n) { window.__badges.push(n); return Promise.resolve(); }, configurable: true });
Object.defineProperty(navigator, 'clearAppBadge', {
  value: function () { window.__badges.push(0); return Promise.resolve(); }, configurable: true });
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    ctx = browser.new_context(viewport={"width": 1100, "height": 720})
    ctx.add_init_script(STUBS)
    pg = ctx.new_page()
    yield pg
    ctx.close()


def open_terminal(page, server, query=""):
    page.goto(f"{server}/terminal{query}")
    page.wait_for_selector(".xterm-screen", timeout=30000)
    # The first session frame proves the tmux client is attached.
    page.wait_for_function(
        "window.MerlinTerminal && window.MerlinTerminal.currentSession()", timeout=30000
    )


@pytest.fixture
def agent_window(tmux_env, server, page):
    """A second, non-current window stamped like an agent window."""
    open_terminal(page, server)
    wid = tmux(
        tmux_env,
        "new-window",
        "-d",
        "-t",
        SESSION,
        "-n",
        "agent",
        "-P",
        "-F",
        "#{window_id}",
    )
    tmux(tmux_env, "set-option", "-w", "-t", wid, "@agent_sid", "sid-e2e-1")
    tmux(tmux_env, "set-option", "-w", "-t", wid, "@agent_cwd", "/tmp/projx")
    tmux(tmux_env, "set-option", "-w", "-t", wid, "@agent_state", "busy")
    yield wid
    subprocess.run(
        ["tmux", "kill-window", "-t", wid], env=tmux_env, capture_output=True
    )


def test_transition_notifies_badges_and_titles(page, server, tmux_env, agent_window):
    wid = agent_window
    page.wait_for_timeout(2500)  # one poll with the window in busy: nothing yet
    assert page.evaluate("window.__notifs.length") == 0

    tmux(tmux_env, "set-option", "-w", "-t", wid, "@agent_state", "done")
    page.wait_for_function("window.__notifs.length === 1", timeout=10000)
    n = page.evaluate("window.__notifs[0]")
    assert n["title"] == "projx · agent"
    assert n["options"]["body"] == "Finished"
    assert n["options"]["tag"] == "sid-e2e-1"
    assert n["options"]["data"]["target"] == f"{SESSION}:{wid}"

    # Badge and title follow the attention count.
    page.wait_for_function("window.__badges.slice(-1)[0] === 1", timeout=10000)
    page.wait_for_function("document.title.startsWith('(1) ')", timeout=10000)

    # The same state seen again is not a transition.
    page.wait_for_timeout(4500)
    assert page.evaluate("window.__notifs.length") == 1

    # A reload replays nothing: the first poll sends no cursor.
    page.reload()
    page.wait_for_selector(".xterm-screen", timeout=30000)
    page.wait_for_function("document.title.startsWith('(1) ')", timeout=10000)
    page.wait_for_timeout(4500)
    assert page.evaluate("window.__notifs.length") == 0

    # Clearing the state clears the badge and the prefix.
    tmux(tmux_env, "set-option", "-w", "-t", wid, "@agent_state", "busy")
    page.wait_for_function("window.__badges.slice(-1)[0] === 0", timeout=10000)
    page.wait_for_function("!document.title.startsWith('(')", timeout=10000)


def test_ask_notifies_with_its_own_body(page, server, tmux_env, agent_window):
    wid = agent_window
    tmux(tmux_env, "set-option", "-w", "-t", wid, "@agent_state", "ask")
    page.wait_for_function("window.__notifs.length === 1", timeout=10000)
    assert page.evaluate("window.__notifs[0].options.body") == "Needs an answer"


def test_current_window_is_not_notified_while_visible(
    page, server, tmux_env, agent_window
):
    wid = agent_window
    # Move this client onto the agent window, then flip it to done.
    page.evaluate(f"window.MerlinTerminal.switchSession('{SESSION}:{wid}')")
    page.wait_for_function(
        f"window.MerlinTerminal.currentWindow() === '{wid}'", timeout=10000
    )
    tmux(tmux_env, "set-option", "-w", "-t", wid, "@agent_state", "done")
    page.wait_for_function("document.title.startsWith('(1) ')", timeout=10000)
    page.wait_for_timeout(3000)
    assert page.evaluate("window.__notifs.length") == 0


def test_deep_link_switches_the_client_and_drops_the_parameter(
    page, server, tmux_env, agent_window
):
    wid = agent_window
    open_terminal(page, server, f"?target={SESSION}:{wid}")
    page.wait_for_function(
        f"window.MerlinTerminal.currentWindow() === '{wid}'", timeout=10000
    )
    assert "target=" not in page.url


# Holds the first session control frame (NUL-prefixed) until the test releases
# it, so the ordering between the deep link and the socket can be observed.
HOLD_FIRST_FRAME = """
(function () {
  const Orig = window.WebSocket;
  window.__frameHeld = false;
  window.__releaseFrame = null;
  class Held extends Orig {
    set onmessage(fn) {
      super.onmessage = (e) => {
        if (!window.__frameHeld && typeof e.data === 'string' && e.data.charCodeAt(0) === 0) {
          window.__frameHeld = true;
          window.__releaseFrame = () => fn(e);
          return;
        }
        fn(e);
      };
    }
    get onmessage() { return super.onmessage; }
  }
  window.WebSocket = Held;
})();
"""


def test_deep_link_is_kept_until_the_socket_is_confirmed(
    browser, server, tmux_env, agent_window
):
    wid = agent_window
    ctx = browser.new_context(viewport={"width": 1100, "height": 720})
    ctx.add_init_script(STUBS)
    ctx.add_init_script(HOLD_FIRST_FRAME)
    pg = ctx.new_page()
    try:
        pg.goto(f"{server}/terminal?target={SESSION}:{wid}")
        pg.wait_for_function("window.__frameHeld === true", timeout=30000)
        pg.wait_for_timeout(500)
        # No confirmed session yet: the target must still be in the URL.
        assert pg.evaluate("window.MerlinTerminal.currentWindow()") == ""
        assert f"target={SESSION}" in pg.url
        pg.evaluate("window.__releaseFrame()")
        pg.wait_for_function(
            f"window.MerlinTerminal.currentWindow() === '{wid}'", timeout=10000
        )
        pg.wait_for_function("!location.search.includes('target=')", timeout=5000)
    finally:
        ctx.close()


def test_overlapping_polls_deliver_an_event_once(page, server, tmux_env, agent_window):
    wid = agent_window
    held = []
    page.route("**/api/board?*", lambda route: held.append(route))
    # A burst of triggers while one request is held: only one may be in flight.
    page.evaluate(
        "() => { for (let i = 0; i < 5; i++) window.SessionsBoard.refresh();"
        " window.dispatchEvent(new Event('focus'));"
        " document.dispatchEvent(new Event('visibilitychange')); }"
    )
    # A poll may already be in flight when the route lands: the burst is then
    # coalesced behind it and the next request is the held one. Waits go
    # through the page (not time.sleep): the sync API only dispatches route
    # handlers while a Playwright call is running.
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(100)
    page.wait_for_timeout(3000)  # the 2s interval fires at least once more
    assert len(held) == 1
    # The transition happens while that request is held.
    tmux(tmux_env, "set-option", "-w", "-t", wid, "@agent_state", "done")
    page.wait_for_timeout(2500)  # let the watcher sweep it
    before = page.evaluate("window.SessionsBoard.cursor()")
    held[0].continue_()
    page.unroute("**/api/board?*")
    page.wait_for_function("window.__notifs.length === 1", timeout=10000)
    after = page.evaluate("window.SessionsBoard.cursor()")
    assert after != before
    # The coalesced follow-up poll and the ones after it replay nothing and
    # never move the cursor backwards.
    page.wait_for_timeout(5000)
    assert page.evaluate("window.__notifs.length") == 1
    later = page.evaluate("window.SessionsBoard.cursor()")
    assert int(later.split(":")[1]) >= int(after.split(":")[1])


def test_popover_toggle_asks_once_and_persists(page, server):
    open_terminal(page, server)
    page.evaluate("localStorage.removeItem('notify-in-browser')")
    # Wrapped in a function: Playwright calls a function-valued expression,
    # and the stub assignment would otherwise be exactly that.
    page.evaluate(
        "() => { window.__asked = 0; Notification.requestPermission = "
        "() => { window.__asked++; return Promise.resolve('granted'); }; }"
    )
    page.click("#notif-btn")
    page.wait_for_selector("#notif-popover.open", timeout=5000)
    toggle = page.locator("#notif-browser-toggle")
    assert not toggle.is_checked()
    assert page.evaluate("window.__asked") == 0  # nothing asked on open
    toggle.check()
    page.wait_for_function(
        "localStorage.getItem('notify-in-browser') === '1'", timeout=5000
    )
    assert page.evaluate("window.__asked") == 1
    assert "On." in page.inner_text("#notif-status")
    toggle.uncheck()
    page.wait_for_function(
        "localStorage.getItem('notify-in-browser') === '0'", timeout=5000
    )
    assert page.evaluate("window.__asked") == 1
    page.keyboard.press("Escape")
    page.wait_for_selector("#notif-popover.open", state="detached", timeout=5000)
