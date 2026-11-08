"""E2E tests for the native terminal's renderer (``/terminal/native``).

tmux's grid of the displayed pane as real DOM text in a native scrolling
container (``terminal/static/native/``, ``terminal/native.py``): styles, wide
characters and links; copy text with soft wraps joined and trailing blanks
trimmed; a scrolled-up view that stays put while output arrives; history
loaded lazily without moving the view; the alternate screen as a layer, with
the mouse for programs that ask for it; the cursor.

Native momentum scrolling and the iOS selection handles cannot be driven by
Playwright: they are on the founder's iPhone checklist (the epic's journal).

Run: uv run scripts.py test-e2e   (or pytest tests/e2e/test_terminal_native.py)
Requires: chromium + tmux.
"""

import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tmux_isolation  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="web terminal needs tmux"
)

ENGINE = "window.MerlinNativeTerminalPage.engine"


@pytest.fixture(scope="module")
def playwright_driver():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright_driver):
    b = playwright_driver.chromium.launch(headless=True)
    yield b
    b.close()


@pytest.fixture(scope="module")
def firefox(playwright_driver):
    """Firefox serialises an empty selection over content-visibility chunks:
    the page's copy handler is its copy path."""
    try:
        b = playwright_driver.firefox.launch(headless=True)
    except Exception as e:  # not installed on this machine
        pytest.skip(f"firefox unavailable: {e}")
    yield b
    b.close()


def _open(browser, server, **context):
    ctx = browser.new_context(
        **(context or {"viewport": {"width": 1000, "height": 700}})
    )
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{server}/terminal/native")
    # Attached, and the pane at the page's size (a later resize would reflow
    # the pane under a test).
    page.wait_for_function(
        f"{ENGINE}.pane && document.querySelector('.nt-screen .r')"
        f" && {ENGINE}.size.cols === {ENGINE}.renderer.cols"
        f" && {ENGINE}.size.rows === {ENGINE}.renderer.rows",
        timeout=20000,
    )
    return ctx, page, errors


@pytest.fixture
def page(browser, server):
    ctx, page, errors = _open(browser, server)
    page.click("#nt-container")
    ready(page)
    run(page, "clear")
    ready(page)
    yield page
    assert errors == []
    ctx.close()


def ready(page) -> None:
    """The shell's prompt is the last line shown, alone: keys typed before the
    shell reads them may be dropped."""
    page.wait_for_function(
        """() => {
            const ls = [...document.querySelectorAll('.nt-screen .l')]
                .map(l => l.textContent.trim()).filter(Boolean);
            return ls.length > 0 && ls[ls.length - 1].endsWith('%');
        }""",
        timeout=20000,
    )


def run(page, command: str) -> None:
    page.keyboard.type(command)
    page.keyboard.press("Enter")


def wait_text(page, text: str, where: str = ".nt-term", timeout: int = 15000) -> None:
    try:
        page.wait_for_function(
            "([sel, t]) => document.querySelector(sel).textContent.includes(t)",
            arg=[where, text],
            timeout=timeout,
        )
    except Exception:
        shown = page.evaluate(
            "sel => [...document.querySelectorAll(sel + ' .l')].filter(l => l.textContent.trim()).slice(-12).map(l => l.textContent)",
            where,
        )
        extra = page.evaluate(
            "() => ({active: document.activeElement && document.activeElement.id, pane: window.MerlinNativeTerminalPage.engine.pane, alt: !document.querySelector('.nt-alt').hidden, n: document.querySelectorAll('.nt-content .l').length})"
        )
        raise AssertionError(f"{text!r} not in {where}: {shown} {extra}") from None


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


def displayed_pane(page) -> str:
    return page.evaluate(f"{ENGINE}.pane")


# -- what a row shows and what it copies ------------------------------------------


def test_styles_wide_characters_and_links_render(page):
    run(
        page,
        r"printf '\033[1;31mBO''LD\033[0m \033[4:3mcur''ly\033[0m \033[7min''v\033[0m "
        r"日本 x \033[38;5;208mo''r\033[0m https://example.com/a?b=1 END''1\n'",
    )
    wait_text(page, "END1", ".nt-content")
    row = page.evaluate(
        """() => {
            const l = [...document.querySelectorAll('.nt-content .l')].find(x => x.textContent.includes('END1'));
            return {
                text: l.textContent,
                bold: !!l.querySelector('span.f9.B'),
                curly: !!l.querySelector('span.U3'),
                inverse: !!l.querySelector('span.gi.fi'),
                wide: [...l.querySelectorAll('span.w')].map(s => s.textContent),
                orange: !!l.querySelector('span[style*="#ff8700"]'),
                href: (l.querySelector('a.nt-link') || {}).href,
                color: getComputedStyle(l.querySelector('span.f9')).color,
            };
        }"""
    )
    assert row["text"] == "BOLD curly inv 日本 x or https://example.com/a?b=1 END1"
    assert row["bold"] and row["curly"] and row["inverse"] and row["orange"]
    assert row["wide"] == ["日", "本"]
    assert row["href"] == "https://example.com/a?b=1"
    assert row["color"] == "rgb(255, 110, 110)"  # bold red drawn bright


@pytest.fixture(params=["chromium", "firefox"])
def any_page(request, server):
    b = request.getfixturevalue("browser" if request.param == "chromium" else "firefox")
    ctx, page, errors = _open(b, server)
    page.click("#nt-container")
    ready(page)
    run(page, "clear")
    ready(page)
    page.engine_name = request.param
    yield page
    assert errors == []
    ctx.close()


def test_copy_joins_soft_wraps_and_trims_trailing_blanks(any_page):
    page = any_page
    cols = page.evaluate(f"{ENGINE}.renderer.cols")
    long = "ab" * (cols // 2 + 7) + "END"  # wraps onto a second row
    run(
        page,
        "python3 -c \"print('ab' * %d + 'E' + 'ND'); print('short   '); print('TA' + 'IL')\""
        % (cols // 2 + 7),
    )
    wait_text(page, "TAIL")
    copied = page.evaluate(
        """() => {
            const ls = [...document.querySelectorAll('.nt-content .l')];
            const a = ls.find(l => l.textContent.startsWith('abab'));
            const b = ls[ls.indexOf(a) + 1];
            // A mouse selection takes the focus off the input field.
            document.activeElement.blur();
            const range = document.createRange();
            range.setStart(a, 0);
            range.setEnd(b, b.childNodes.length);
            const sel = getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            // What the clipboard gets, read inside the (trusted) copy event,
            // after the page's own handler.
            window.__copied = null;
            window.addEventListener('copy', (e) => { window.__copied = e.clipboardData.getData('text/plain'); }, {once: true});
            return {rows: a.querySelectorAll('.r').length, dom: sel.toString()};
        }"""
    )
    page.keyboard.press("Control+c")
    page.wait_for_function("window.__copied !== null")
    copied["handler"] = page.evaluate("window.__copied")
    assert copied["rows"] == 2  # one logical line, two visual rows
    # The copy handler (desktop, every browser) and the DOM's own text (what
    # iOS copies; Chromium serialises it exactly, Firefox does not over
    # content-visibility chunks, which is why the handler exists).
    assert copied["handler"] == long + "\nshort"
    if page.engine_name == "chromium":
        assert copied["dom"].rstrip("\n") == long + "\nshort"


# -- scrolling --------------------------------------------------------------------


def test_a_scrolled_up_view_stays_put_while_output_arrives(page, tmux_env):
    run(page, "seq 1 3000")
    wait_text(page, "3000", ".nt-screen")
    wait_text(page, "2900", ".nt-hist")
    before = page.evaluate(
        """() => {
            const s = document.querySelector('.nt-scroller');
            s.scrollTop = Math.floor(s.scrollHeight / 2);
            return null;
        }"""
    )
    page.wait_for_timeout(200)
    before = page.evaluate(
        """() => {
            const s = document.querySelector('.nt-scroller');
            const r = s.getBoundingClientRect();
            const el = document.elementFromPoint(r.left + 10, r.top + 30);
            return {top: s.scrollTop, text: el && el.closest('.l').textContent, stuck: window.MerlinNativeTerminalPage.engine.stuck};
        }"""
    )
    assert before["stuck"] is False
    assert page.is_visible("#nt-jump")
    # Output the page does not type (typing returns to the bottom).
    tmux(tmux_env, "send-keys", "-t", displayed_pane(page), "seq 5001 5400", "Enter")
    wait_text(page, "5400", ".nt-screen")
    page.wait_for_timeout(300)
    after = page.evaluate(
        """() => {
            const s = document.querySelector('.nt-scroller');
            const r = s.getBoundingClientRect();
            const el = document.elementFromPoint(r.left + 10, r.top + 30);
            return {top: s.scrollTop, text: el && el.closest('.l').textContent};
        }"""
    )
    assert after == {"top": before["top"], "text": before["text"]}
    page.click("#nt-jump")
    page.wait_for_function(
        """() => { const s = document.querySelector('.nt-scroller');
                   return s.scrollTop + s.clientHeight >= s.scrollHeight - 4; }"""
    )
    assert not page.is_visible("#nt-jump")
    # Scrolled up again, a typed key brings the view back down.
    page.evaluate("document.querySelector('.nt-scroller').scrollTop = 100")
    page.wait_for_function(f"!{ENGINE}.stuck")
    page.keyboard.type("x")
    page.wait_for_function(f"{ENGINE}.stuck")
    page.keyboard.press("Backspace")


def test_history_loads_lazily_without_moving_the_view(browser, server):
    ctx, page, errors = _open(browser, server)
    page.click("#nt-container")
    ready(page)
    run(page, "clear; seq 1 6000")
    wait_text(page, "6000", ".nt-screen")
    page.reload()
    page.wait_for_function(
        f"{ENGINE}.pane && {ENGINE}.renderer.spacerRows > 0", timeout=20000
    )
    state = page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer;
            return {{rows: r.histRows, spacer: r.spacerRows, height: r.scroller.scrollHeight}}; }}"""
    )
    assert state["rows"] == 1000  # a reconnect starts with the last 1000 rows
    # Each step: the view a little above the rows the page has.
    for _ in range(40):
        done = page.evaluate(
            f"""() => {{ const r = {ENGINE}.renderer, s = r.scroller;
                const before = s.scrollTop;
                s.scrollTop = Math.max(0, r.spacerHeight() - s.clientHeight);
                return r.spacerRows === 0; }}"""
        )
        if done:
            break
        page.wait_for_timeout(150)
    final = page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer;
            const nums = [...document.querySelectorAll('.nt-hist .l')].map(l => l.textContent)
                .filter(t => /^\\d+$/.test(t)).map(Number);
            return {{spacer: r.spacerRows, height: r.scroller.scrollHeight, nums}}; }}"""
    )
    assert final["spacer"] == 0
    # Every row once, in order, and the content height never changed: the
    # spacer gave back exactly the height of the rows that replaced it.
    seq = [n for n in final["nums"] if 1 <= n <= 6000]
    assert seq[:3] == [1, 2, 3] and seq == sorted(set(seq))
    assert [n for n in range(1, 5900) if n not in set(seq)] == []
    assert final["height"] == state["height"]
    assert errors == []
    ctx.close()


# -- the alternate screen, the mouse ------------------------------------------------

PROBE = textwrap.dedent(
    """
    import os, sys, termios, tty
    out, mouse = sys.argv[1], sys.argv[2] == "mouse"
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    w = sys.stdout.write
    w("\\033[?1049h\\033[H")
    if mouse:
        w("\\033[?1000h\\033[?1006h")
    w("ALT " + ("MOUSE" if mouse else "KEYS") + " READY\\r\\n")
    sys.stdout.flush()
    try:
        while True:
            data = os.read(fd, 1024)
            with open(out, "ab") as f:
                f.write(data)
            if b"q" in data:
                break
    finally:
        w("\\033[?1000l\\033[?1006l\\033[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    """
)


def _probe(page, tmp_path: Path, mode: str) -> Path:
    script = tmp_path / "probe.py"
    script.write_text(PROBE)
    out = tmp_path / f"{mode}.bin"
    run(page, f"python3 {script} {out} {mode}")
    page.wait_for_function(
        "t => { const a = document.querySelector('.nt-alt');"
        " return !a.hidden && a.textContent.includes(t); }",
        arg=f"ALT {'MOUSE' if mode == 'mouse' else 'KEYS'} READY",
        timeout=15000,
    )
    return out


def _read_until(path: Path, needle: bytes, timeout: float = 5.0) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = path.read_bytes() if path.exists() else b""
        if needle in data:
            return data
        time.sleep(0.05)
    return path.read_bytes() if path.exists() else b""


def test_the_alternate_screen_is_a_layer_and_gets_the_mouse_it_asked_for(
    page, tmp_path
):
    run(page, "echo MAIN''MARK")
    wait_text(page, "MAINMARK", ".nt-content")
    out = _probe(page, tmp_path, "mouse")
    cell = page.evaluate(f"({{w: {ENGINE}.renderer.cellW, h: {ENGINE}.renderer.rowH}})")
    box = page.locator(".nt-alt").bounding_box()
    # Column 5, row 3 (1-based).
    page.mouse.click(box["x"] + cell["w"] * 4.5, box["y"] + cell["h"] * 2.5)
    data = _read_until(out, b"\x1b[<0;5;3m")
    assert b"\x1b[<0;5;3M" in data and b"\x1b[<0;5;3m" in data
    page.mouse.move(box["x"] + 20, box["y"] + 20)
    page.mouse.wheel(0, -60)
    assert b"\x1b[<64;" in _read_until(out, b"\x1b[<64;")
    page.keyboard.press("q")
    page.wait_for_function("document.querySelector('.nt-alt').hidden")
    # Back on the main screen, what was there before is there again.
    wait_text(page, "MAINMARK", ".nt-content")


def test_without_the_mouse_a_wheel_in_the_alternate_screen_sends_arrows(page, tmp_path):
    out = _probe(page, tmp_path, "keys")
    box = page.locator(".nt-alt").bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 20)
    page.mouse.wheel(0, -60)
    page.mouse.wheel(0, 60)
    data = _read_until(out, b"\x1b[B")
    assert b"\x1b[A" in data and b"\x1b[B" in data and b"\x1b[<" not in data
    page.keyboard.press("q")
    page.wait_for_function("document.querySelector('.nt-alt').hidden")


# -- the cursor ---------------------------------------------------------------------


def test_the_cursor_is_an_overlay_that_hides_with_the_program_cursor(page):
    page.wait_for_function(
        "document.querySelector('.nt-screen .nt-cursor, .nt-hist .nt-cursor')"
    )
    shape = page.evaluate("document.querySelector('.nt-cursor').dataset.shape")
    assert shape == "block"
    # The cursor adds no text to its row.
    assert page.evaluate("document.querySelector('.nt-cursor').textContent") == ""
    run(page, r"printf '\033[?25l'; sleep 3; printf '\033[?25h'")
    page.wait_for_function("!document.querySelector('.nt-cursor')", timeout=5000)
    page.wait_for_function("document.querySelector('.nt-cursor')", timeout=8000)


# -- a phone ------------------------------------------------------------------------


def test_on_a_phone_the_grid_fits_and_a_tap_focuses_the_field(browser, server):
    ctx, page, errors = _open(
        browser,
        server,
        viewport={"width": 390, "height": 664},
        is_mobile=True,
        has_touch=True,
        device_scale_factor=2,
    )
    page.tap("#nt-container")
    assert page.evaluate("document.activeElement.id") == "nt-input"
    page.wait_for_function(
        f"{ENGINE}.renderer.cols === {ENGINE}.size.cols && {ENGINE}.size.cols < 70"
    )
    fit = page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer, s = r.scroller;
            const rows = [...document.querySelectorAll('.nt-screen .r')];
            const wide = [...s.querySelectorAll('*')].filter(e => e.getBoundingClientRect().right > s.getBoundingClientRect().right + 1).slice(0, 3).map(e => e.className + ':' + e.getBoundingClientRect().right + ':' + (e.style.cssText || ''));
            return {{wide, client: s.clientWidth, content: r.content.style.minWidth, cellW: r.cellW, overflow: s.scrollWidth - s.clientWidth,
                     heights: [...new Set(rows.map(x => x.getBoundingClientRect().height))],
                     rowH: r.rowH, cols: r.cols, size: {ENGINE}.size}}; }}"""
    )
    assert fit["overflow"] == 0, fit
    assert fit["heights"] == [fit["rowH"]]  # every row exactly one row height
    assert fit["cols"] == fit["size"]["cols"]
    assert errors == []
    ctx.close()


SWIPE = """([dy, steps]) => {
    const alt = document.querySelector('.nt-alt');
    const r = alt.getBoundingClientRect();
    const x = r.left + 40;
    let y = r.top + r.height / 2;
    const touch = (yy) => new Touch({identifier: 1, target: alt, clientX: x, clientY: yy});
    const fire = (type, list) => alt.dispatchEvent(new TouchEvent(type, {
        touches: type === 'touchend' ? [] : list, changedTouches: list,
        bubbles: true, cancelable: true}));
    fire('touchstart', [touch(y)]);
    for (let i = 0; i < steps; i++) { y += dy; fire('touchmove', [touch(y)]); }
    fire('touchend', [touch(y)]);
}"""


@pytest.mark.parametrize("mode", ["mouse", "keys"])
def test_a_swipe_in_the_alternate_screen_scrolls_the_program(
    browser, server, tmp_path, mode
):
    """A finger moving down asks for earlier lines: wheel up when the program
    asked for the mouse, the Up arrow otherwise; a tap is a click."""
    ctx, page, errors = _open(
        browser,
        server,
        viewport={"width": 390, "height": 664},
        is_mobile=True,
        has_touch=True,
    )
    page.tap("#nt-container")
    ready(page)
    out = _probe(page, tmp_path, mode)
    page.evaluate(SWIPE, [10, 6])  # down: earlier lines
    page.evaluate(SWIPE, [-10, 6])  # up: later lines
    if mode == "mouse":
        data = _read_until(out, b"\x1b[<65;")
        assert b"\x1b[<64;" in data and b"\x1b[<65;" in data
        page.tap(".nt-alt")
        assert b"\x1b[<0;" in _read_until(out, b"\x1b[<0;")
    else:
        data = _read_until(out, b"\x1b[B")
        assert b"\x1b[A" in data and b"\x1b[B" in data and b"\x1b[<" not in data
    page.keyboard.press("q")
    page.wait_for_function("document.querySelector('.nt-alt').hidden")
    assert errors == []
    ctx.close()


def test_old_history_goes_back_to_the_spacer_and_loads_again(page):
    """Past its cap the page gives its oldest chunks back to the spacer (the
    same height), and they load again from tmux when scrolled to."""
    page.evaluate(f"{ENGINE}.renderer.opts.maxRows = 600")
    run(page, "seq 1 3000")
    wait_text(page, "3000", ".nt-screen")
    page.wait_for_function(
        f"{ENGINE}.renderer.histRows <= 800 && {ENGINE}.renderer.spacerRows > 1500"
    )
    state = page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer;
            return {{rows: r.histRows, spacer: r.spacerRows, oldest: r.oldest, next: r.next,
                     height: r.scroller.scrollHeight}}; }}"""
    )
    assert state["oldest"] + state["rows"] == state["next"]
    page.evaluate(f"{ENGINE}.renderer.opts.maxRows = 100000")
    for _ in range(40):
        if page.evaluate(
            f"""() => {{ const r = {ENGINE}.renderer, s = r.scroller;
                s.scrollTop = Math.max(0, r.spacerHeight() - s.clientHeight);
                return r.spacerRows === 0; }}"""
        ):
            break
        page.wait_for_timeout(150)
    nums = page.evaluate(
        """() => [...document.querySelectorAll('.nt-hist .l')].map(l => l.textContent)
            .filter(t => /^\\d+$/.test(t)).map(Number)"""
    )
    assert nums[:2] == [1, 2] and nums == sorted(set(nums))
    assert [n for n in range(1, 2900) if n not in set(nums)] == []
    after = page.evaluate(f"""() => {{ const r = {ENGINE}.renderer;
            return {{rows: r.histRows, spacer: r.spacerRows, oldest: r.oldest, next: r.next,
                     height: r.scroller.scrollHeight}}; }}""")
    assert after["height"] == state["height"], (state, after)


def test_rows_tmux_dropped_show_as_a_gap_and_trimming_passes_it(page, tmux_env):
    """A page that stops drawing while its pane floods past tmux's history
    limit gets the rows tmux still has and a marker for the rest (no text to
    copy); trimming later removes a chunk holding such a marker."""
    tmux(tmux_env, "set", "-g", "history-limit", "300")
    tmux(tmux_env, "new-window")
    page.wait_for_function(f"{ENGINE}.renderer.histRows === 0", timeout=10000)
    ready(page)
    page.evaluate(f"window.__drain = {ENGINE}.drain; {ENGINE}.drain = () => {{}}")
    run(page, "seq 1 20000; echo FL''OOD-END")
    pane = displayed_pane(page)
    deadline = time.monotonic() + 20
    while "FLOOD-END" not in tmux(tmux_env, "capture-pane", "-p", "-t", pane):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    page.evaluate(f"{ENGINE}.drain = window.__drain; {ENGINE}.drain()")
    wait_text(page, "FLOOD-END", ".nt-screen")
    page.wait_for_function("document.querySelector('.nt-gap')")
    gap = page.evaluate(
        """() => { const g = document.querySelector('.nt-gap');
            return {rows: +g.dataset.rows, text: g.textContent}; }"""
    )
    assert gap["rows"] > 1000 and gap["text"] == ""
    # Trimming down to a few rows removes the chunk with the marker.
    page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer; r.opts.maxRows = 50; r.opts.chunk = 50; }}"""
    )
    run(page, "seq 1 400")
    wait_text(page, "400", ".nt-screen")
    page.wait_for_function("!document.querySelector('.nt-gap')", timeout=10000)
    state = page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer;
            return {{rows: r.histRows, oldest: r.oldest, next: r.next, first: r.firstHeld,
                     spacer: r.spacerRows}}; }}"""
    )
    assert state["oldest"] + state["rows"] == state["next"]
    assert state["spacer"] == max(0, state["oldest"] - state["first"])
    tmux(tmux_env, "kill-window")
    tmux(tmux_env, "set", "-g", "history-limit", "50000")
