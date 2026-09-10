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


# ---------------------------------------------------------------------------
# M2: the installable app
# ---------------------------------------------------------------------------


def test_manifest_is_served_with_its_type(page, server):
    r = page.request.get(f"{server}/manifest.webmanifest")
    assert r.status == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    body = r.json()
    assert body["display"] == "standalone"
    assert body["start_url"] == "/terminal"


def test_worker_registers_and_never_intercepts(page, server):
    open_terminal(page, server)
    scope = page.evaluate("navigator.serviceWorker.ready.then(r => r.scope)")
    assert scope == f"{server}/"
    # Once the worker controls the page, a request still goes to the network.
    page.reload()
    page.wait_for_selector(".xterm-screen", timeout=30000)
    page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=10000)
    with page.expect_response("**/api/notifications/status") as resp:
        page.evaluate("fetch('/api/notifications/status')")
    assert resp.value.from_service_worker is False
    with page.expect_response("**/terminal") as resp:
        page.reload()
    assert resp.value.from_service_worker is False


def test_installability_criteria(page, server):
    """Chromium's own verdict on the manifest, recorded for the journal."""
    open_terminal(page, server)
    page.evaluate("navigator.serviceWorker.ready")
    cdp = page.context.new_cdp_session(page)
    manifest = cdp.send("Page.getAppManifest")
    assert manifest["errors"] == []
    errors = cdp.send("Page.getInstallabilityErrors")["installabilityErrors"]
    print("installability errors:", errors)
    ids = {e["errorId"] for e in errors}
    # Headless has no user engagement and may not run the full audit, but the
    # manifest itself must raise nothing.
    manifest_errors = {
        "manifest-missing-name-or-short-name",
        "manifest-missing-suitable-icon",
        "manifest-display-not-supported",
        "manifest-empty",
        "start-url-not-valid",
        "no-icon-available",
        "no-matching-service-worker",
        "no-manifest",
    }
    assert not (ids & manifest_errors), errors


def test_popover_says_when_there_is_no_tmux_server(page, server):
    open_terminal(page, server)
    page.evaluate(
        "() => { const orig = window.fetch; window.fetch = (u, o) => "
        "String(u).includes('/api/notifications/status') ? "
        "Promise.resolve(new Response(JSON.stringify({tmux: false, swept: true, cursor: 'x:0'}), "
        "{headers: {'Content-Type': 'application/json'}})) : orig(u, o); }"
    )
    page.click("#notif-btn")
    page.wait_for_selector("#notif-notice:not([hidden])", timeout=5000)
    assert "tmux server" in page.inner_text("#notif-notice")
    assert page.locator("#notif-browser-toggle").is_hidden()


def test_popover_on_an_iphone_browser_asks_to_install_first(browser, server):
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        is_mobile=True,
        has_touch=True,
    )
    ctx.add_init_script(STUBS)
    pg = ctx.new_page()
    try:
        open_terminal(pg, server)
        pg.click("#notif-btn")
        pg.wait_for_selector("#notif-popover.open", timeout=5000)
        assert "Home Screen" in pg.inner_text("#notif-push-slot")
    finally:
        ctx.close()


def test_reconnect_probe_after_an_absence(page, server):
    open_terminal(page, server)
    hide = (
        "() => { Object.defineProperty(document, 'hidden', {get: () => true, configurable: true});"
        " document.dispatchEvent(new Event('visibilitychange')); }"
    )
    show = (
        "() => { Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});"
        " document.dispatchEvent(new Event('visibilitychange')); }"
    )
    # Short absence: a probe, a pong, the socket kept.
    page.evaluate(hide)
    page.wait_for_timeout(2500)
    page.evaluate(show)
    page.wait_for_function(
        "window.MerlinTerminal.probeStats().pongs === 1", timeout=10000
    )
    stats = page.evaluate("window.MerlinTerminal.probeStats()")
    assert stats["probes"] == 1 and stats["replaced"] == 0
    # Long absence (the clock jumps a while the page is hidden): replaced.
    page.evaluate(hide)
    page.evaluate(
        "() => { const real = Date.now; window.__realNow = real; Date.now = () => real() + 120000; }"
    )
    page.evaluate(show)
    page.wait_for_function(
        "window.MerlinTerminal.probeStats().replaced === 1", timeout=10000
    )
    page.evaluate("() => { Date.now = window.__realNow; }")
    page.wait_for_function(
        "document.getElementById('status-dot').className === 'connected'", timeout=15000
    )
    page.wait_for_function("window.MerlinTerminal.currentSession()", timeout=15000)


# The first socket dies on its probe (a phone's half-open socket, but closing
# right away so the ordinary reconnect path runs). The reconnect must win and
# the probe's deadline must not tear the new socket down.
DEAD_ON_PING = """
(function () {
  const Orig = window.WebSocket;
  let first = true;
  class DeadOnPing extends Orig {
    constructor(...a) { super(...a); this.__dead = first; first = false; }
    send(d) {
      if (this.__dead && typeof d === 'string' && d.includes('"ping"')) { this.close(); return; }
      super.send(d);
    }
  }
  window.WebSocket = DeadOnPing;
})();
"""


def test_probe_deadline_never_hits_the_reconnected_socket(browser, server):
    ctx = browser.new_context(viewport={"width": 1100, "height": 720})
    ctx.add_init_script(DEAD_ON_PING)
    pg = ctx.new_page()
    try:
        open_terminal(pg, server)
        pg.evaluate(
            "() => { Object.defineProperty(document, 'hidden', {get: () => true, configurable: true});"
            " document.dispatchEvent(new Event('visibilitychange')); }"
        )
        pg.wait_for_timeout(2500)
        pg.evaluate(
            "() => { Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});"
            " document.dispatchEvent(new Event('visibilitychange')); }"
        )
        # The probe kills the socket, the ordinary reconnect brings a new one.
        pg.wait_for_function(
            "window.MerlinTerminal.probeStats().probes === 1", timeout=5000
        )
        pg.wait_for_function(
            "document.getElementById('status-dot').className === 'disconnected'"
            " || document.getElementById('status-dot').className === 'connecting'",
            timeout=5000,
        )
        pg.wait_for_function(
            "document.getElementById('status-dot').className === 'connected'",
            timeout=15000,
        )
        # Past the original probe deadline: still the same healthy socket.
        pg.wait_for_timeout(4000)
        stats = pg.evaluate("window.MerlinTerminal.probeStats()")
        assert stats == {"probes": 1, "pongs": 0, "replaced": 0}
        assert (
            pg.evaluate("document.getElementById('status-dot').className")
            == "connected"
        )
        # And the new socket answers a probe normally.
        pg.evaluate(
            "() => { Object.defineProperty(document, 'hidden', {get: () => true, configurable: true});"
            " document.dispatchEvent(new Event('visibilitychange')); }"
        )
        pg.wait_for_timeout(2500)
        pg.evaluate(
            "() => { Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});"
            " document.dispatchEvent(new Event('visibilitychange')); }"
        )
        pg.wait_for_function(
            "window.MerlinTerminal.probeStats().pongs === 1", timeout=10000
        )
        assert pg.evaluate("window.MerlinTerminal.probeStats().replaced") == 0
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# M3: Web Push through the popover, against a push service run by the test
# ---------------------------------------------------------------------------

import base64
import http.server
import json
import threading


@pytest.fixture(scope="module")
def push_service():
    """A stand-in push service: records every POST and answers 201."""
    records = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            headers = {k.lower(): v for k, v in self.headers.items()}
            records.append({"path": self.path, "headers": headers, "body": body})
            self.send_response(201)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", records
    httpd.shutdown()


def fake_subscription_keys():
    """Real P-256 keys, so the server's encryption is exercised for real."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    priv = ec.generate_private_key(ec.SECP256R1())
    raw = priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    return {"p256dh": b64(raw), "auth": b64(os.urandom(16))}


def push_stub(endpoint, keys):
    """PushManager as the popover sees it, on the real registration. The
    fake keeps its subscription in sessionStorage so a reload sees it, and
    window.__unsubscribeFails makes the browser refuse to drop it."""
    return """
(function () {
  const ENDPOINT = %s, KEYS = %s;
  const KEY = 'fake-push-sub';
  function make() {
    return {
      endpoint: ENDPOINT,
      toJSON: () => ({ endpoint: ENDPOINT, keys: KEYS }),
      unsubscribe: async () => {
        if (window.__unsubscribeFails) throw new Error('browser refused');
        sessionStorage.removeItem(KEY); return true;
      },
    };
  }
  const fake = {
    getSubscription: async () => (sessionStorage.getItem(KEY) ? make() : null),
    subscribe: async (opts) => {
      window.__subscribeOpts = { userVisibleOnly: opts.userVisibleOnly, keyLen: opts.applicationServerKey.length };
      sessionStorage.setItem(KEY, '1');
      return make();
    },
  };
  Object.defineProperty(ServiceWorkerRegistration.prototype, 'pushManager', { get: () => fake, configurable: true });
})();
""" % (json.dumps(endpoint), json.dumps(keys))


def test_push_toggle_subscribes_tests_and_unsubscribes(browser, server, push_service):
    base, records = push_service
    endpoint = f"{base}/push/e2e-1"
    ctx = browser.new_context(viewport={"width": 1100, "height": 720})
    ctx.add_init_script(STUBS)
    ctx.add_init_script(push_stub(endpoint, fake_subscription_keys()))
    pg = ctx.new_page()
    try:
        open_terminal(pg, server)
        pg.evaluate("navigator.serviceWorker.ready")
        pg.click("#notif-btn")
        # Right after the first attach the watcher may still report no tmux
        # server: the popover re-checks every two seconds until it clears.
        pg.wait_for_selector("#notif-push-toggle", timeout=15000)
        toggle = pg.locator("#notif-push-toggle")
        assert not toggle.is_checked()
        assert pg.locator("#notif-test-btn").is_disabled()
        toggle.check()
        pg.wait_for_selector("#notif-devices .notif-device-me", timeout=10000)
        opts = pg.evaluate("window.__subscribeOpts")
        assert opts == {"userVisibleOnly": True, "keyLen": 65}
        devices = pg.request.get(f"{server}/api/notifications/devices").json()[
            "devices"
        ]
        assert [d["endpoint"] for d in devices] == [endpoint]
        assert devices[0]["label"]  # derived from the user agent or the client hints

        # A test push goes through the real sender to the fake service.
        pg.click("#notif-test-btn")
        pg.wait_for_function(
            "document.getElementById('notif-status').textContent.includes('Test sent')",
            timeout=15000,
        )
        assert len(records) == 1
        h = records[0]["headers"]
        assert records[0]["path"] == "/push/e2e-1"
        assert h["ttl"] == "300"
        assert h["urgency"] == "high"
        assert h["content-encoding"] == "aes128gcm"
        assert h["authorization"].startswith("vapid ")
        assert len(records[0]["body"]) > 0
        devices = pg.request.get(f"{server}/api/notifications/devices").json()[
            "devices"
        ]
        assert devices[0]["last_success"]

        toggle.uncheck()
        pg.wait_for_function("!document.getElementById('notif-devices')", timeout=10000)
        assert (
            pg.request.get(f"{server}/api/notifications/devices").json()["devices"]
            == []
        )
    finally:
        ctx.close()


def _open_bell(pg, server):
    open_terminal(pg, server)
    pg.evaluate("navigator.serviceWorker.ready")
    pg.click("#notif-btn")
    pg.wait_for_selector("#notif-push-toggle", timeout=15000)


def test_failed_registration_rolls_the_browser_subscription_back(
    browser, server, push_service
):
    base, _ = push_service
    ctx = browser.new_context(viewport={"width": 1100, "height": 720})
    ctx.add_init_script(STUBS)
    ctx.add_init_script(push_stub(f"{base}/push/rollback", fake_subscription_keys()))
    pg = ctx.new_page()
    try:
        _open_bell(pg, server)
        pg.route(
            "**/api/notifications/subscribe",
            lambda route: route.fulfill(status=500, body="{}"),
        )
        pg.locator(
            "#notif-push-toggle"
        ).click()  # not check(): the box must not flip here
        pg.wait_for_function(
            "document.getElementById('notif-status').textContent.includes('Could not subscribe')",
            timeout=10000,
        )
        assert not pg.locator("#notif-push-toggle").is_checked()
        assert (
            pg.evaluate("sessionStorage.getItem('fake-push-sub')") is None
        )  # rolled back
        pg.unroute("**/api/notifications/subscribe")
        # A reload and reopen agree: nothing is on, the instance lists no device.
        pg.reload()
        _open_bell(pg, server)
        assert not pg.locator("#notif-push-toggle").is_checked()
        assert (
            pg.request.get(f"{server}/api/notifications/devices").json()["devices"]
            == []
        )
    finally:
        ctx.close()


def test_browser_refusing_to_unsubscribe_keeps_both_sides_on(
    browser, server, push_service
):
    base, _ = push_service
    endpoint = f"{base}/push/keep"
    ctx = browser.new_context(viewport={"width": 1100, "height": 720})
    ctx.add_init_script(STUBS)
    ctx.add_init_script(push_stub(endpoint, fake_subscription_keys()))
    pg = ctx.new_page()
    try:
        _open_bell(pg, server)
        pg.locator(
            "#notif-push-toggle"
        ).click()  # not check(): the box must not flip here
        pg.wait_for_selector("#notif-devices .notif-device-me", timeout=10000)
        pg.evaluate("window.__unsubscribeFails = true")
        pg.locator("#notif-push-toggle").click()  # not uncheck(): the box must stay on
        pg.wait_for_function(
            "document.getElementById('notif-status').textContent.includes('kept the subscription')",
            timeout=10000,
        )
        assert pg.locator("#notif-push-toggle").is_checked()
        devices = pg.request.get(f"{server}/api/notifications/devices").json()[
            "devices"
        ]
        assert [d["endpoint"] for d in devices] == [endpoint]
        # After a reload the state is still consistent: on, with the device listed.
        pg.reload()
        _open_bell(pg, server)
        assert pg.locator("#notif-push-toggle").is_checked()
        # And a browser-only subscription (server record gone) reads as off.
        pg.request.delete(
            f"{server}/api/notifications/subscribe", data={"endpoint": endpoint}
        )
        pg.reload()
        _open_bell(pg, server)
        assert not pg.locator("#notif-push-toggle").is_checked()
        assert pg.evaluate("sessionStorage.getItem('fake-push-sub')") == "1"
    finally:
        ctx.close()


def test_registered_device_reads_as_on_before_the_bell_is_opened(
    browser, server, push_service
):
    base, _ = push_service
    endpoint = f"{base}/push/preload"
    ctx = browser.new_context(viewport={"width": 1100, "height": 720})
    ctx.add_init_script(STUBS)
    ctx.add_init_script(push_stub(endpoint, fake_subscription_keys()))
    pg = ctx.new_page()
    try:
        _open_bell(pg, server)
        pg.locator("#notif-push-toggle").check()
        pg.wait_for_selector("#notif-devices .notif-device-me", timeout=10000)
        pg.reload()
        open_terminal(pg, server)
        # The hint dot (permission granted, no push) must settle to hidden
        # without any tap, since this device is on both sides.
        pg.wait_for_function(
            "document.getElementById('notif-dot').hidden === true", timeout=10000
        )
        pg.click("#notif-btn")
        pg.wait_for_selector("#notif-push-toggle", timeout=15000)
        assert pg.locator("#notif-push-toggle").is_checked()
        pg.locator("#notif-push-toggle").uncheck()
        pg.wait_for_function("!document.getElementById('notif-devices')", timeout=10000)
    finally:
        ctx.close()


def test_failed_registration_then_a_successful_retry_in_the_same_popover(
    browser, server, push_service
):
    base, _ = push_service
    endpoint = f"{base}/push/retry"
    ctx = browser.new_context(viewport={"width": 1100, "height": 720})
    ctx.add_init_script(STUBS)
    ctx.add_init_script(push_stub(endpoint, fake_subscription_keys()))
    pg = ctx.new_page()
    try:
        _open_bell(pg, server)
        pg.route(
            "**/api/notifications/subscribe",
            lambda route: route.fulfill(status=500, body="{}"),
        )
        pg.locator("#notif-push-toggle").click()
        pg.wait_for_function(
            "document.getElementById('notif-status').textContent.includes('Could not subscribe')",
            timeout=10000,
        )
        pg.unroute("**/api/notifications/subscribe")
        pg.locator("#notif-push-toggle").click()
        pg.wait_for_selector("#notif-devices .notif-device-me", timeout=10000)
        assert pg.locator("#notif-push-toggle").is_checked()
        status = pg.inner_text("#notif-status")
        assert "Could not subscribe" not in status
        assert "error" not in (pg.get_attribute("#notif-status", "class") or "")
        pg.locator("#notif-push-toggle").uncheck()
        pg.wait_for_function("!document.getElementById('notif-devices')", timeout=10000)
    finally:
        ctx.close()
