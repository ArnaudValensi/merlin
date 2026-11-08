"""E2E tests for the native terminal's parity with the classic one, and the
switch between them.

The native page (``/terminal/native``, ``terminal/static/native/page.js``)
does what the classic page does around its terminal, through the same seams
(``window.MerlinTerminal``, the Sessions panel, the shared status bar and
panels): the window tabs with their agent-state pills, the deep link and the
push-tap relay, the paste button and a right-click, uploads (picker, drop, a
pasted image), ``clipboard_sync``, a program's copy (desktop and the phone's
Copy pill), the cd and commits buttons, voice input and its target, the
notices, the toolbar toggle, the keyboard suggestions setting, the reconnect
probe. The switch (``terminal-choice.js``): classic by default, a per-browser
choice that ``/terminal`` follows without opening a classic socket, the menu,
the URL parameter, and the fall back to the classic page when tmux is too old.

The software keyboard following the visual viewport and the standalone
padding cannot be produced by Playwright: they are on the founder's iPhone
checklist (the epic's journal).

Run: uv run scripts.py test-e2e   (or pytest tests/e2e/test_terminal_native_parity.py)
Requires: chromium + tmux.
"""

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tmux_isolation  # noqa: E402
from conftest import start_merlin, stop_merlin  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="web terminal needs tmux"
)

# Voice needs a transcription backend for the mic button to record at all; the
# uploads are intercepted, so the key is never used.
MERLIN_OPTIONS = {"extra_env": {"OPENAI_API_KEY": "sk-e2e-test"}}

ENGINE = "window.MerlinNativeTerminalPage.engine"
SCREEN = f"{ENGINE}.renderer.screenText()"
DESKTOP = {"viewport": {"width": 1100, "height": 720}}


@pytest.fixture(scope="module")
def playwright_driver():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright_driver):
    b = playwright_driver.chromium.launch(
        headless=True,
        args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
    )
    yield b
    b.close()


def tmux(env: dict, *args: str) -> str:
    """A command on the test server's private tmux, aimed by its socket."""
    sock = tmux_isolation.socket_path(env["TMUX_TMPDIR"])
    clean = {k: v for k, v in env.items() if k != "TMUX"}
    return subprocess.run(
        ["tmux", "-S", str(sock), *args],
        env=clean,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _native(page, server, query: str = "") -> None:
    page.goto(f"{server}/terminal/native{query}")
    wait_native(page)


def wait_native(page) -> None:
    page.wait_for_function(
        f"window.MerlinNativeTerminalPage && {ENGINE}.pane"
        " && window.MerlinTerminal.currentWindow()"
        f" && {ENGINE}.size.cols === {ENGINE}.renderer.cols",
        timeout=20000,
    )


def ready(page) -> None:
    """The shell's prompt is the last line shown."""
    page.wait_for_function(
        f"""() => {{
            const ls = {SCREEN}.split('\\n').map(l => l.trim()).filter(Boolean);
            return ls.length > 0 && ls[ls.length - 1].endsWith('%');
        }}""",
        timeout=20000,
    )


def clear_line(page) -> None:
    page.click("#nt-container")
    page.keyboard.press("Control+u")
    page.keyboard.type("clear")
    page.keyboard.press("Enter")
    ready(page)


def count(page, marker: str) -> int:
    return page.evaluate(SCREEN).count(marker)


def wait_count(page, marker: str, n: int, timeout: int = 8000) -> None:
    page.wait_for_function(
        f"m => {SCREEN}.split(m).length - 1 >= {n}", arg=marker, timeout=timeout
    )


def wait_value(page, expression: str, expected, timeout: float = 8.0) -> None:
    """Until ``expression`` evaluates to ``expected``. For anything async:
    wait_for_function takes a returned promise for a truthy value."""
    deadline = time.monotonic() + timeout
    value = page.evaluate(expression)
    while value != expected and time.monotonic() < deadline:
        page.wait_for_timeout(50)
        value = page.evaluate(expression)
    assert value == expected


def status(page) -> str:
    return " ".join((page.text_content("#terminal-status") or "").split())


def sent_frames(page) -> list:
    """Every JSON message the page sends on its terminal socket from now on."""
    frames: list = []

    def on_ws(ws):
        def sent(payload):
            if isinstance(payload, str) and payload.startswith("{"):
                frames.append(json.loads(payload))

        ws.on("framesent", sent)

    page.on("websocket", on_ws)
    return frames


@pytest.fixture
def page(browser, server):
    ctx = browser.new_context(
        **DESKTOP, permissions=["clipboard-read", "clipboard-write"]
    )
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    _native(pg, server)
    clear_line(pg)
    yield pg
    assert errors == []
    ctx.close()


def new_window(env: dict) -> str:
    session = tmux(env, "display", "-p", "-t", "merlin-dev", "#{session_name}").strip()
    return tmux(
        env, "new-window", "-d", "-t", session, "-P", "-F", "#{window_id}"
    ).strip()


def kill_window(env: dict, wid: str) -> None:
    tmux(env, "kill-window", "-t", wid)


def pane_line(env: dict, pane: str) -> str:
    """The pane's screen with newlines dropped (zsh breaks a long line)."""
    return tmux(env, "capture-pane", "-p", "-t", pane).replace("\n", "")


# -- the switch -----------------------------------------------------------------------


def test_the_switch_is_classic_by_default_and_follows_the_choice(
    browser, server, tmux_env
):
    ctx = browser.new_context(**DESKTOP)
    pg = ctx.new_page()
    sockets: list[str] = []
    pg.on("websocket", lambda ws: sockets.append(ws.url))
    pg.goto(f"{server}/terminal")
    pg.wait_for_selector(".xterm-screen", timeout=30000)
    pg.wait_for_function("window.MerlinTerminal.currentWindow()", timeout=20000)
    assert pg.evaluate("!window.MerlinNativeTerminalPage")
    assert pg.evaluate("localStorage.getItem('merlin-terminal')") is None

    # ?terminal=native: remembered, and the rest of the URL (a deep link)
    # carried to the native page, which follows it.
    wid = new_window(tmux_env)
    try:
        session = pg.evaluate("window.MerlinTerminal.currentSession()")
        pg.goto(f"{server}/terminal?terminal=native&target={session}:{wid}")
        wait_native(pg)
        pg.wait_for_function(
            f"window.MerlinTerminal.currentWindow() === '{wid}'", timeout=10000
        )
        pg.wait_for_function("!location.search.includes('target')", timeout=5000)
        assert "terminal=" not in pg.url
        assert pg.evaluate("localStorage.getItem('merlin-terminal')") == "native"

        # /terminal now leads to the native page without a classic socket.
        sockets.clear()
        pg.goto(f"{server}/terminal")
        wait_native(pg)
        assert pg.url.endswith("/terminal/native")
        assert [
            u for u in sockets if "/ws/terminal?" in u or u.endswith("/ws/terminal")
        ] == []

        # The menu: the current page checked, Classic goes back and is kept.
        pg.click("#term-menu-btn")
        checked = pg.evaluate(
            "[...document.querySelectorAll('#term-menu [data-terminal]')]"
            ".map(i => i.dataset.terminal + '=' + i.getAttribute('aria-checked'))"
        )
        assert checked == ["classic=false", "native=true"]
        pg.click("#term-menu [data-terminal=classic]")
        pg.wait_for_selector(".xterm-screen", timeout=30000)
        assert pg.url.endswith("/terminal")
        assert pg.evaluate("localStorage.getItem('merlin-terminal')") == "classic"

        # ?terminal=classic on the native page goes back too.
        pg.goto(f"{server}/terminal?terminal=native")
        wait_native(pg)
        pg.goto(f"{server}/terminal/native?terminal=classic")
        pg.wait_for_selector(".xterm-screen", timeout=30000)
        assert pg.evaluate("localStorage.getItem('merlin-terminal')") == "classic"
    finally:
        kill_window(tmux_env, wid)
        ctx.close()


def test_a_tmux_too_old_sends_the_tab_back_to_the_classic_page(browser, server):
    ctx = browser.new_context(**DESKTOP)
    pg = ctx.new_page()
    pg.goto(f"{server}/terminal?terminal=native")
    wait_native(pg)
    # What the bridge sends on a tmux older than it needs (Phase 2).
    pg.evaluate(
        f"{ENGINE}.opts.onControl({{type: 'unsupported', version: '3.4', minimum: '3.8'}})"
    )
    pg.wait_for_selector(".term-fallback", timeout=20000)
    assert pg.url.endswith("/terminal")
    assert "tmux 3.8" in pg.text_content(".term-fallback")
    assert "3.4" in pg.text_content(".term-fallback")
    pg.wait_for_selector(".xterm-screen", timeout=30000)
    # The tab stays on the classic page; the choice itself is kept.
    pg.goto(f"{server}/terminal")
    pg.wait_for_selector(".xterm-screen", timeout=30000)
    assert pg.url.endswith("/terminal")
    assert pg.evaluate("localStorage.getItem('merlin-terminal')") == "native"
    pg.click("#term-menu-btn")
    assert "tmux 3.8" in pg.text_content("#term-menu-note")
    # Asking for the new terminal again tries it again.
    pg.goto(f"{server}/terminal?terminal=native")
    wait_native(pg)
    ctx.close()


def test_a_direct_visit_on_an_old_tmux_says_why_without_changing_the_choice(
    browser, server
):
    for saved in (None, "classic"):
        ctx = browser.new_context(**DESKTOP)
        if saved:
            ctx.add_init_script(
                f"if (!sessionStorage.getItem('seeded')) {{ sessionStorage.setItem('seeded', '1');"
                f" localStorage.setItem('merlin-terminal', '{saved}'); }}"
            )
        pg = ctx.new_page()
        _native(pg, server)
        pg.evaluate(
            f"{ENGINE}.opts.onControl({{type: 'unsupported', version: '3.4', minimum: '3.8'}})"
        )
        pg.wait_for_selector(".term-fallback", timeout=20000)
        assert pg.url.endswith("/terminal")
        assert "tmux 3.8" in pg.text_content(".term-fallback")
        assert pg.evaluate("localStorage.getItem('merlin-terminal')") == saved
        # Said once: the next classic load, with Classic still the choice, is
        # plain.
        pg.reload()
        pg.wait_for_selector(".xterm-screen", timeout=30000)
        pg.wait_for_timeout(300)
        assert pg.query_selector(".term-fallback") is None
        # The native page attaching again clears the old fall back: its
        # classic page has nothing to say afterwards either.
        _native(pg, server)
        assert pg.evaluate("sessionStorage.getItem('merlin-terminal-fallback')") is None
        ctx.close()


# -- windows ----------------------------------------------------------------------------


def _tab(page, wid: str) -> dict:
    return page.evaluate(
        """wid => { const t = document.querySelector(`.nt-tab[data-window="${wid}"]`);
            if (!t) return null;
            const p = t.querySelector('.nt-pill');
            return {glyph: p.textContent, color: getComputedStyle(p).color,
                    active: t.classList.contains('active'), name: t.textContent}; }""",
        wid,
    )


def test_window_tabs_show_the_agent_state_pills_and_switch(page, tmux_env):
    first = page.evaluate("window.MerlinTerminal.currentWindow()")
    states = {"busy": None, "ask": None, "done": None, "idle": None}
    try:
        for state in states:
            wid = new_window(tmux_env)
            tmux(tmux_env, "set", "-w", "-t", wid, "@agent_state", state)
            states[state] = wid
        page.wait_for_function(
            "n => document.querySelectorAll('.nt-tab').length === n",
            arg=len(states) + 1,
            timeout=10000,
        )
        # The glyph and colour rules of tmux.conf.
        expected = {
            "busy": ("◐", "rgb(249, 226, 175)"),
            "ask": ("?", "rgb(137, 220, 235)"),
            "done": ("●", "rgb(166, 227, 161)"),
            "idle": ("○", "rgb(98, 114, 164)"),
        }
        for state, wid in states.items():
            page.wait_for_function(
                f"""() => {{ const p = document.querySelector('.nt-tab[data-window="{wid}"] .nt-pill');
                    return p && p.textContent === '{expected[state][0]}'; }}""",
                timeout=10000,
            )
            tab = _tab(page, wid)
            assert (tab["glyph"], tab["color"]) == expected[state], state
            assert not tab["active"]
        # The current window without an agent: the classic filled circle.
        tab = _tab(page, first)
        assert tab["active"] and tab["glyph"] == "●"
        assert tab["color"] == "rgb(189, 147, 249)"

        # A tap on a tab selects its window, and the field keeps the focus.
        page.click(f'.nt-tab[data-window="{states["busy"]}"]')
        page.wait_for_function(
            f"window.MerlinTerminal.currentWindow() === '{states['busy']}'",
            timeout=10000,
        )
        page.wait_for_function(
            f"""() => document.querySelector('.nt-tab[data-window="{states["busy"]}"]').classList.contains('active')""",
            timeout=10000,
        )
        assert page.evaluate("document.activeElement.id") == "nt-input"
        # The idle agent's pill turns purple when its window is active.
        page.click(f'.nt-tab[data-window="{states["idle"]}"]')
        page.wait_for_function(
            f"""() => {{ const t = document.querySelector('.nt-tab[data-window="{states["idle"]}"]');
                return t.classList.contains('active')
                    && getComputedStyle(t.querySelector('.nt-pill')).color === 'rgb(189, 147, 249)'; }}""",
            timeout=10000,
        )
    finally:
        for wid in states.values():
            if wid:
                kill_window(tmux_env, wid)
        page.evaluate(
            "t => window.MerlinTerminal.switchSession(t)", f"merlin-dev:{first}"
        )


def test_the_sessions_panel_and_a_push_tap_switch_the_window(page, tmux_env):
    first = page.evaluate("window.MerlinTerminal.currentWindow()")
    session = page.evaluate("window.MerlinTerminal.currentSession()")
    wid = new_window(tmux_env)
    try:
        # The docked Sessions panel lists the new window: a click switches.
        page.evaluate("window.SessionsBoard.refresh()")
        page.wait_for_selector(
            f'#sessions-panel .wrow[data-window="{wid}"]', timeout=10000
        )
        page.click(f'#sessions-panel .wrow[data-window="{wid}"]')
        page.wait_for_function(
            f"window.MerlinTerminal.currentWindow() === '{wid}'", timeout=10000
        )
        # A tapped push notification, relayed by the service worker.
        page.evaluate(
            """t => navigator.serviceWorker.dispatchEvent(
                new MessageEvent('message', {data: {type: 'deep-link', target: t}}))""",
            f"{session}:{first}",
        )
        page.wait_for_function(
            f"window.MerlinTerminal.currentWindow() === '{first}'", timeout=10000
        )
    finally:
        kill_window(tmux_env, wid)


def test_the_docked_panel_divider_refits_the_terminal(page):
    cols = page.evaluate(f"{ENGINE}.size.cols")
    box = page.locator("#sessions-divider").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 150, y, steps=6)
    page.mouse.up()
    page.wait_for_function(f"{ENGINE}.size.cols < {cols} - 10", timeout=5000)
    assert page.evaluate("localStorage.getItem('sessions-w')")


def test_on_a_phone_the_sessions_button_toggles_the_sheet(
    playwright_driver, browser, server
):
    iphone = {
        k: v
        for k, v in playwright_driver.devices["iPhone 13"].items()
        if k != "default_browser_type"
    }
    ctx = browser.new_context(**iphone)
    pg = ctx.new_page()
    _native(pg, server)
    assert pg.is_visible("#sessions-btn")
    pg.tap("#sessions-btn")
    pg.wait_for_selector("#sessions-panel.open", timeout=5000)
    pg.wait_for_selector("#sessions-panel .wrow", timeout=10000)
    pg.tap("#sessions-btn")
    pg.wait_for_selector("#sessions-panel:not(.open)", state="attached", timeout=5000)
    # Every button of the status bar fits on the phone's screen.
    right = pg.evaluate(
        "Math.max(...[...document.querySelectorAll('#terminal-status > *')]"
        ".filter(e => e.offsetParent).map(e => e.getBoundingClientRect().right))"
    )
    assert right <= pg.viewport_size["width"]
    ctx.close()


# -- paste and copy -----------------------------------------------------------------------


def test_the_paste_button_and_a_right_click_paste_once_and_sync(page):
    frames = sent_frames(page)
    # A new socket, so the recorder sees its frames.
    page.evaluate(f"{ENGINE}.connect()")
    wait_native(page)
    ready(page)

    page.evaluate("t => navigator.clipboard.writeText(t)", "PASTE_BUTTON_MARK")
    page.click("#paste-btn")
    wait_count(page, "PASTE_BUTTON_MARK", 1)
    page.wait_for_timeout(500)
    assert count(page, "PASTE_BUTTON_MARK") == 1
    assert "Pasted!" in status(page)
    assert {"type": "clipboard_sync", "text": "PASTE_BUTTON_MARK"} in frames
    assert page.evaluate("window.MerlinTerminal.lastPasteResult().kind") == "text"
    clear_line(page)

    # A right-click without a selection pastes, as in the classic page.
    page.evaluate("t => navigator.clipboard.writeText(t)", "RIGHT_CLICK_MARK")
    page.click("#nt-container", button="right")
    wait_count(page, "RIGHT_CLICK_MARK", 1)
    page.wait_for_timeout(500)
    assert count(page, "RIGHT_CLICK_MARK") == 1
    clear_line(page)

    # With a selection it is the browser's menu: nothing is pasted.
    page.keyboard.type("echo SELECT_ME")
    page.keyboard.press("Enter")
    wait_count(page, "SELECT_ME", 2)
    box = page.evaluate(
        """() => { const row = [...document.querySelectorAll('.nt-term .r')]
                .find(x => x.textContent.trim() === 'SELECT_ME');
            const r = document.createRange(); r.selectNodeContents(row);
            const b = r.getBoundingClientRect();
            return {x: b.left, y: b.top + b.height / 2, w: b.width}; }"""
    )
    page.mouse.move(box["x"] + 1, box["y"])
    page.mouse.down()
    page.mouse.move(box["x"] + box["w"] - 1, box["y"], steps=5)
    page.mouse.up()
    assert page.evaluate("getSelection().toString().trim()") == "SELECT_ME"
    page.evaluate(
        "() => { window.__prevented = null; addEventListener('contextmenu',"
        " e => { window.__prevented = e.defaultPrevented; }, {once: true}); }"
    )
    page.mouse.click(box["x"] + box["w"] / 2, box["y"], button="right")
    assert page.evaluate("window.__prevented") is False
    page.wait_for_timeout(800)
    assert count(page, "RIGHT_CLICK_MARK") == 0
    page.evaluate("getSelection().removeAllRanges()")


FAKE_DENIED_READ = """() => {
    window.__readTextCalls = 0;
    Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: {
            read: async () => { throw Object.assign(new Error('denied'), { name: 'NotAllowedError' }); },
            readText: async () => { window.__readTextCalls++; return 'must not be used'; },
            writeText: async () => {},
        },
    });
}"""


def test_a_refused_read_is_reported_and_not_asked_twice(page):
    page.evaluate(FAKE_DENIED_READ)
    page.click("#paste-btn")
    page.wait_for_function(
        "window.MerlinTerminal.lastPasteResult() !== null", timeout=5000
    )
    assert page.evaluate("window.__readTextCalls") == 0
    assert page.evaluate("window.MerlinTerminal.lastPasteResult().reason") == "blocked"
    assert "Clipboard blocked" in status(page)
    assert count(page, "must not be used") == 0


def test_a_program_copy_reaches_the_clipboard_on_a_desktop(page, tmux_env):
    tmux(tmux_env, "set-buffer", "-b", "merlin-clip", "PROGRAM_COPY_MARK")
    wait_value(page, "navigator.clipboard.readText()", "PROGRAM_COPY_MARK")
    # And Ctrl+Shift+C with nothing selected copies it again (synced).
    frames = sent_frames(page)
    page.evaluate(f"{ENGINE}.connect()")
    wait_native(page)
    page.evaluate("navigator.clipboard.writeText('other')")
    page.click("#nt-container")
    page.keyboard.press("Control+Shift+C")
    wait_value(page, "navigator.clipboard.readText()", "PROGRAM_COPY_MARK")
    assert {"type": "clipboard_sync", "text": "PROGRAM_COPY_MARK"} in frames


def test_a_program_copy_on_a_phone_offers_the_copy_pill(
    playwright_driver, browser, server, tmux_env
):
    iphone = {
        k: v
        for k, v in playwright_driver.devices["iPhone 13"].items()
        if k != "default_browser_type"
    }
    ctx = browser.new_context(
        **iphone, permissions=["clipboard-read", "clipboard-write"]
    )
    pg = ctx.new_page()
    frames = sent_frames(pg)
    _native(pg, server)
    tmux(tmux_env, "set-buffer", "-b", "merlin-clip", "PHONE_COPY_MARK")
    pg.wait_for_selector("#copy-toast:not([hidden])", timeout=10000)
    # merlin-clip paste in the container gets it at once.
    assert {"type": "clipboard_sync", "text": "PHONE_COPY_MARK"} in frames
    pg.tap("#copy-toast")
    wait_value(pg, "navigator.clipboard.readText()", "PHONE_COPY_MARK")
    assert pg.is_hidden("#copy-toast")
    ctx.close()


# -- uploads -------------------------------------------------------------------------------

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _paths(line: str, names: list[str]) -> list[str]:
    pattern = r"/tmp/merlin-clipboard/[0-9a-f]{6}-(?:%s)" % "|".join(
        re.escape(n) for n in names
    )
    return [Path(p).name.split("-", 1)[1] for p in re.findall(pattern, line)]


def test_uploads_type_their_paths_in_order(page, tmux_env):
    pane = page.evaluate(f"{ENGINE}.pane")
    names = ["pick-one.png", "pick-two.png", "pick-three.png"]
    page.set_input_files(
        "#upload-file-input",
        [{"name": n, "mimeType": "image/png", "buffer": PNG_BYTES} for n in names],
    )
    page.wait_for_function(
        "() => document.getElementById('status-text').textContent === '3 files ready'",
        timeout=5000,
    )
    page.wait_for_timeout(500)
    assert _paths(pane_line(tmux_env, pane), names) == names
    clear_line(page)

    # Dropped on the terminal.
    drop = ["drop-a.txt", "drop-b.txt"]
    page.evaluate(
        """(names) => {
            const dt = new DataTransfer();
            for (const n of names) dt.items.add(new File([n], n, { type: 'text/plain' }));
            document.getElementById('nt-container').dispatchEvent(
                new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
        }""",
        drop,
    )
    page.wait_for_function(
        "() => document.getElementById('status-text').textContent === '2 files ready'",
        timeout=5000,
    )
    page.wait_for_timeout(500)
    assert _paths(pane_line(tmux_env, pane), drop) == drop
    clear_line(page)

    # An image pasted into the field (the browser's own paste event).
    page.evaluate(
        """(bytes) => {
            const dt = new DataTransfer();
            dt.items.add(new File([new Uint8Array(bytes)], 'shot.png', { type: 'image/png' }));
            document.getElementById('nt-input').dispatchEvent(
                new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
        }""",
        list(PNG_BYTES),
    )
    page.wait_for_function(
        "() => document.getElementById('status-text').textContent === 'File ready'",
        timeout=5000,
    )
    page.wait_for_timeout(500)
    found = _paths(pane_line(tmux_env, pane), ["shot.png"])
    assert found == ["shot.png"]
    path = re.search(
        r"/tmp/merlin-clipboard/[0-9a-f]{6}-shot\.png", pane_line(tmux_env, pane)
    )
    assert Path(path.group(0)).read_bytes() == PNG_BYTES


# -- cd, commits, the toolbar, the menu ---------------------------------------------------


def test_the_cd_and_commits_buttons(page, tmp_path):
    target = tmp_path / "cd here"
    target.mkdir()
    page.click("#cd-btn")
    page.wait_for_selector("#folder-picker-modal", state="visible", timeout=5000)
    # The picker answers with a folder: the page types the quoted cd.
    page.evaluate("p => FolderPicker.open(p)", str(target))
    page.wait_for_function(
        "t => document.getElementById('picker-use-btn').textContent.includes('cd here')",
        arg=str(target),
        timeout=10000,
    )
    page.click("#picker-use-btn")
    page.keyboard.type("pwd")
    page.keyboard.press("Enter")
    wait_count(page, f"\n{target}", 1)

    # The commits button in a folder that is not a repository says so.
    page.click("#commits-btn")
    page.wait_for_function(
        "() => document.getElementById('status-text').textContent === 'Not a git repo'",
        timeout=5000,
    )
    # In the checkout it opens the commits of its repository.
    root = str(Path(__file__).resolve().parents[2])
    page.keyboard.type(f"cd '{root}'")
    page.keyboard.press("Enter")
    ready(page)
    page.click("#commits-btn")
    page.wait_for_url(re.compile(r".*/commits\?repo=.*"), timeout=10000)


def test_the_toolbar_toggle_gives_the_rows_back(page):
    rows = page.evaluate(f"{ENGINE}.size.rows")
    page.click("#toolbar-toggle")
    assert page.is_hidden("#nt-toolbar")
    page.wait_for_function(f"{ENGINE}.size.rows > {rows}", timeout=5000)
    assert "active" not in page.get_attribute("#toolbar-toggle", "class")
    page.click("#toolbar-toggle")
    assert page.is_visible("#nt-toolbar")
    page.wait_for_function(f"{ENGINE}.size.rows === {rows}", timeout=5000)


def test_the_menu_turns_the_keyboard_suggestions_on_and_off(page):
    page.click("#term-menu-btn")
    item = "#term-menu-suggestions"
    assert page.get_attribute(item, "aria-checked") == "false"
    page.click(item)
    assert page.get_attribute(item, "aria-checked") == "true"
    assert page.get_attribute("#nt-input", "autocorrect") == "on"
    assert (
        page.evaluate("localStorage.getItem(MerlinNativeInput.SUGGESTIONS_KEY)") == "1"
    )
    page.click(item)
    assert page.get_attribute("#nt-input", "autocorrect") == "off"
    page.keyboard.press("Escape")
    assert page.is_hidden("#term-menu")


# -- voice ---------------------------------------------------------------------------------


def _record(page) -> bytes:
    with page.expect_request("**/api/terminal/transcribe", timeout=20000) as req:
        page.click("#mic-btn")
        page.wait_for_timeout(700)
        page.click("#mic-btn")
    return req.value.post_data_buffer or b""


def test_voice_input_targets_the_stopped_window_and_types_a_fallback(browser, server):
    ctx = browser.new_context(**DESKTOP, permissions=["microphone"])
    pg = ctx.new_page()
    answer = {"status": 202, "body": '{"status": "accepted"}'}
    pg.route(
        "**/api/terminal/transcribe",
        lambda route: route.fulfill(
            status=answer["status"],
            content_type="application/json",
            body=answer["body"],
        ),
    )
    _native(pg, server)
    clear_line(pg)
    target = pg.evaluate("window.MerlinTerminal.currentTarget()")
    assert ":@" in target
    body = _record(pg)
    assert b'name="target"' in body and target.encode() in body
    pg.wait_for_function(
        "!document.getElementById('mic-btn').classList.contains('uploading')",
        timeout=10000,
    )

    # 200: the text comes back and the page types it.
    answer.update(status=200, body='{"text": "VOICE_FALLBACK_MARK"}')
    _record(pg)
    wait_count(pg, "VOICE_FALLBACK_MARK", 1)
    clear_line(pg)

    # A final refusal is a notice over the terminal.
    answer.update(status=422, body='{"error": "Audio unreadable here"}')
    _record(pg)
    pg.wait_for_selector("#nt-notice:not([hidden])", timeout=10000)
    assert "Audio unreadable here" in pg.text_content("#nt-notice")
    assert count(pg, "Audio unreadable") == 0
    # The seam the voice console uses.
    pg.click("#nt-notice")
    pg.evaluate("window.MerlinTerminal.notice('Seam notice')")
    assert pg.text_content("#nt-notice") == "Seam notice"
    ctx.close()


def _audio_of(body: bytes) -> bytes:
    """The recording inside a multipart upload."""
    boundary = body.split(b"\r\n", 1)[0]
    for part in body.split(boundary):
        if b'name="file"' in part:
            return part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
    raise AssertionError("no recording in the upload")


PENDING_VOICE = """() => new Promise((resolve, reject) => {
    const req = indexedDB.open('merlin-voice', 1);
    req.onerror = () => reject(req.error);
    req.onsuccess = () => {
        const db = req.result;
        const c = db.transaction('audio', 'readonly').objectStore('audio').count('pending_voice');
        c.onsuccess = () => { db.close(); resolve(c.result); };
    };
})"""


def _failed_recording(browser, server):
    """A page whose recording failed all three uploads: the recording is kept
    and the mic offers a retry."""
    ctx = browser.new_context(**DESKTOP, permissions=["microphone"])
    pg = ctx.new_page()
    answer = {"status": 500, "body": "{}"}
    bodies: list[bytes] = []

    def handle(route):
        bodies.append(route.request.post_data_buffer or b"")
        route.fulfill(
            status=answer["status"],
            content_type="application/json",
            body=answer["body"],
        )

    pg.route("**/api/terminal/transcribe", handle)
    _native(pg, server)
    target = pg.evaluate("window.MerlinTerminal.currentTarget()")
    pg.click("#mic-btn")
    pg.wait_for_timeout(700)
    pg.click("#mic-btn")
    pg.wait_for_selector("#mic-btn.retry-pending", timeout=20000)
    assert len(bodies) == 3
    assert pg.evaluate(PENDING_VOICE) == 1
    return ctx, pg, answer, bodies, target


def test_a_failed_recording_survives_a_reload_and_keeps_its_window(
    browser, server, tmux_env
):
    ctx, pg, answer, bodies, target = _failed_recording(browser, server)
    wid = new_window(tmux_env)
    try:
        audio = _audio_of(bodies[0])
        assert audio

        # Back after a reload, the retry is still offered.
        pg.reload()
        wait_native(pg)
        pg.wait_for_selector("#mic-btn.retry-pending", timeout=10000)
        assert pg.evaluate(PENDING_VOICE) == 1

        # In another window by now: the text still belongs to the first.
        session = pg.evaluate("window.MerlinTerminal.currentSession()")
        pg.evaluate("t => window.MerlinTerminal.switchSession(t)", f"{session}:{wid}")
        pg.wait_for_function(
            f"window.MerlinTerminal.currentWindow() === '{wid}'", timeout=10000
        )

        answer.update(status=202, body='{"status": "accepted"}')
        pg.click("#mic-btn")
        pg.wait_for_function(
            "() => { const c = document.getElementById('mic-btn').classList;"
            " return !c.contains('retry-pending') && !c.contains('uploading'); }",
            timeout=10000,
        )
        assert len(bodies) == 4
        assert target.encode() in bodies[3]
        assert wid.encode() not in bodies[3]
        assert _audio_of(bodies[3]) == audio
        wait_value(pg, PENDING_VOICE, 0)
    finally:
        kill_window(tmux_env, wid)
        ctx.close()


def test_a_refused_retry_drops_the_recording(browser, server):
    ctx, pg, answer, bodies, _ = _failed_recording(browser, server)
    answer.update(status=422, body='{"error": "Audio unreadable here"}')
    pg.click("#mic-btn")
    pg.wait_for_selector("#nt-notice:not([hidden])", timeout=10000)
    assert "Audio unreadable here" in pg.text_content("#nt-notice")
    wait_value(pg, PENDING_VOICE, 0)
    assert "retry-pending" not in (pg.get_attribute("#mic-btn", "class") or "")
    # Final: no attempt after the one refused.
    pg.wait_for_timeout(2500)
    assert len(bodies) == 4
    ctx.close()


# -- the connection --------------------------------------------------------------------------


def test_the_reconnect_probe_and_a_dropped_socket(page):
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
    # Long absence: replaced at once.
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
    wait_native(page)
    # A socket the server drops: the page says so and reconnects.
    page.evaluate(f"{ENGINE}.ws.close()")
    page.wait_for_function(
        "document.getElementById('status-text').textContent.startsWith('Reconnecting')",
        timeout=5000,
    )
    page.wait_for_function(
        "document.getElementById('status-dot').className === 'connected'"
        " && window.MerlinTerminal.currentWindow()",
        timeout=15000,
    )
    wait_native(page)


@pytest.fixture(scope="module")
def locked(tmp_path_factory):
    """A second server, with a password."""
    server = start_merlin(tmp_path_factory, password="native-pw", name="locked")
    yield server.url
    stop_merlin(server)


def test_an_expired_login_stops_the_reconnects(browser, locked):
    ctx = browser.new_context(**DESKTOP)
    pg = ctx.new_page()
    pg.goto(f"{locked}/login")
    pg.fill('input[name="password"]', "native-pw")
    pg.click('button[type="submit"]')
    pg.wait_for_load_state("networkidle")
    _native(pg, locked)
    sockets: list[str] = []
    pg.on("websocket", lambda ws: sockets.append(ws.url))
    # The login is gone (expired, signed out elsewhere): the socket that
    # replaces this one is refused with 4401, and the page stops there.
    ctx.clear_cookies()
    pg.evaluate(f"{ENGINE}.ws.close()")
    pg.wait_for_function(
        "document.getElementById('status-text').textContent.startsWith('Unauthorized')",
        timeout=15000,
    )
    assert pg.get_attribute("#status-dot", "class") == "disconnected"
    n = len(sockets)
    pg.wait_for_timeout(2500)
    assert len(sockets) == n
    ctx.close()
