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
import re
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

# Every row the page has, in DOM order, is numbered one more than the row
# before it (a gap marker aside), from the oldest to the screen's last row.
CONTIGUOUS = """() => {
    const r = window.MerlinNativeTerminalPage.engine.renderer;
    const nums = [...document.querySelectorAll('.nt-hist .r')].map(x => x._n);
    const gaps = document.querySelectorAll('.nt-hist .nt-gap, .nt-hist .nt-pending').length;
    const ok = nums.every((n, i) => i === 0 || n === nums[i - 1] + 1);
    return ok && gaps === 0 && nums[0] === r.oldest && nums.length === r.rowCount
        && nums[nums.length - 1] === r.screenTop + r.rows - 1;
}"""


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
    # The Sessions panel collapsed: these tests want the terminal's width
    # (column 100 and more), not the panel's.
    ctx.add_init_script("localStorage.setItem('sessions-collapsed', '1')")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{server}/terminal/native")
    # Attached, and the pane at the page's size (a later resize would reflow
    # the pane under a test).
    page.wait_for_function(
        f"{ENGINE}.pane && document.querySelector('.nt-hist .r')"
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
            const ls = window.MerlinNativeTerminalPage.engine.renderer.screenText()
                .split('\\n').map(l => l.trim()).filter(Boolean);
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
            "([sel, t]) => (sel === 'screen'"
            " ? window.MerlinNativeTerminalPage.engine.renderer.screenText()"
            " : document.querySelector(sel).textContent).includes(t)",
            arg=[where, text],
            timeout=timeout,
        )
    except Exception:
        shown = page.evaluate(
            "sel => sel === 'screen' ? window.MerlinNativeTerminalPage.engine.renderer.screenText()"
            " : [...document.querySelectorAll(sel + ' .l')].filter(l => l.textContent.trim()).slice(-12).map(l => l.textContent)",
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


def test_text_a_program_links_to_a_web_address_opens_it(page):
    page.context.route(
        "https://example.com/**",
        lambda r: r.fulfill(body="ok", content_type="text/plain"),
    )
    run(
        page,
        r"printf '\033]8;;https://example.com/att\033\\see docs\033]8;;\033\\ "
        r"\033]8;;file:///etc/hostname\033\\a path\033]8;;\033\\ END''2\n'",
    )
    wait_text(page, "END2", ".nt-content")
    assert page.locator("a.nt-link", has_text="a path").count() == 0
    with page.context.expect_page() as opened:
        page.locator("a.nt-link", has_text="see docs").last.click()
    assert opened.value.url == "https://example.com/att"
    opened.value.close()


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
    wait_text(page, "3000", "screen")
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
    wait_text(page, "5400", "screen")
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
    wait_text(page, "6000", "screen")
    page.reload()
    page.wait_for_function(
        f"{ENGINE}.pane && {ENGINE}.renderer.spacerRows > 0", timeout=20000
    )
    state = page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer;
            return {{rows: r.next - r.oldest, spacer: r.spacerRows, height: r.scroller.scrollHeight}}; }}"""
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
    out, mode = sys.argv[1], sys.argv[2]
    modes = {"mouse": "1000;1006", "x10": "1000", "motion": "1003;1006"}
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    w = sys.stdout.write
    w("\\033[?1049h\\033[H")
    for m in modes.get(mode, "").split(";"):
        if m:
            w("\\033[?" + m + "h")
    w("ALT " + mode.upper() + " READY\\r\\n")
    sys.stdout.flush()
    try:
        while True:
            data = os.read(fd, 1024)
            with open(out, "ab") as f:
                f.write(data)
            if b"\\x03" in data:  # Ctrl+C: no mouse report holds it
                break
    finally:
        w("\\033[?1000l\\033[?1003l\\033[?1006l\\033[?1049l")
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
        arg=f"ALT {mode.upper()} READY",
        timeout=15000,
    )
    return out


def quit_probe(page) -> None:
    page.keyboard.press("Control+c")
    page.wait_for_function("document.querySelector('.nt-alt').hidden")


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
    quit_probe(page)
    # Back on the main screen, what was there before is there again.
    wait_text(page, "MAINMARK", ".nt-content")


def _cell_point(page, col: int, row: int) -> tuple[float, float]:
    """The middle of a cell of the alternate layer (1-based)."""
    cell = page.evaluate(f"({{w: {ENGINE}.renderer.cellW, h: {ENGINE}.renderer.rowH}})")
    box = page.locator(".nt-alt").bounding_box()
    return box["x"] + cell["w"] * (col - 0.5), box["y"] + cell["h"] * (row - 0.5)


def test_x10_reports_reach_the_program_as_bytes_past_column_95(page, tmp_path):
    """Mode 1000 without 1005 or 1006: each value is one byte, 32 + value, so
    column 100 is byte 0x84, which no text message can carry."""
    assert page.evaluate(f"{ENGINE}.renderer.cols") > 100
    out = _probe(page, tmp_path, "x10")
    page.mouse.click(*_cell_point(page, 100, 3))
    data = _read_until(out, b"\x1b[M#")
    assert b"\x1b[M " + bytes([32 + 100, 32 + 3]) in data  # press, left button
    assert b"\x1b[M#" + bytes([32 + 100, 32 + 3]) in data  # release
    quit_probe(page)


def test_all_motion_is_reported_once_per_cell_with_the_motion_bit(page, tmp_path):
    """Mode 1003: a move with no button is button code 35 (3 + 32), one
    report per cell entered."""
    out = _probe(page, tmp_path, "motion")
    x, y = _cell_point(page, 5, 3)
    cell_w = page.evaluate(f"{ENGINE}.renderer.cellW")
    for dx in (-0.3, 0, 0.3):  # three moves inside column 5
        page.mouse.move(x + dx * cell_w, y)
    for dx in (0.8, 1.0, 1.2):  # three inside column 6
        page.mouse.move(x + dx * cell_w, y)
    data = _read_until(out, b"\x1b[<35;6;3M")
    page.wait_for_timeout(300)
    data = out.read_bytes()
    assert data.count(b"\x1b[<35;5;3M") == 1 and data.count(b"\x1b[<35;6;3M") == 1, data
    quit_probe(page)


def test_without_the_mouse_a_wheel_in_the_alternate_screen_sends_arrows(page, tmp_path):
    out = _probe(page, tmp_path, "keys")
    box = page.locator(".nt-alt").bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 20)
    page.mouse.wheel(0, -60)
    page.mouse.wheel(0, 60)
    data = _read_until(out, b"\x1b[B")
    assert b"\x1b[A" in data and b"\x1b[B" in data and b"\x1b[<" not in data
    quit_probe(page)


# -- the cursor ---------------------------------------------------------------------


def test_the_cursor_is_an_overlay_that_hides_with_the_program_cursor(page):
    page.wait_for_function("document.querySelector('.nt-hist .nt-cursor')")
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
            const rows = [...document.querySelectorAll('.nt-hist .r')].slice(-20);
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


def _touch(page, cdp, points: list[tuple[float, float]]) -> None:
    """Real touch input (what a phone sends: touch, pointer and, after a tap,
    compatibility mouse events): down at the first point, moves through the
    others, up at the last."""
    (x, y), *rest = points
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
    )
    for x, y in rest:
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]},
        )
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


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
    cdp = page.context.new_cdp_session(page)
    box = page.locator(".nt-alt").bounding_box()
    x, y = box["x"] + 60, box["y"] + 300
    _touch(page, cdp, [(x, y + 10 * i) for i in range(8)])  # down: earlier lines
    _touch(page, cdp, [(x, y - 10 * i) for i in range(8)])  # up: later lines
    if mode == "mouse":
        data = _read_until(out, b"\x1b[<65;")
        assert b"\x1b[<64;" in data and b"\x1b[<65;" in data
        assert b"\x1b[<0;" not in data  # a swipe is not a click
        # A tap is one click: one press and one release, although the browser
        # follows the touch with compatibility mouse events.
        _touch(page, cdp, [(x, y)])
        # The compatibility mouse events a phone fires after a tap (headless
        # Chromium does not generate them from CDP touch): mouse events with no
        # pointer events.
        page.evaluate(
            """([x, y]) => { const alt = document.querySelector('.nt-alt');
                for (const t of ['mousemove', 'mousedown', 'mouseup', 'click'])
                    alt.dispatchEvent(new MouseEvent(t, {clientX: x, clientY: y, bubbles: true, button: 0})); }""",
            [x, y],
        )
        data = _read_until(out, b"\x1b[<0;")
        page.wait_for_timeout(500)
        data = out.read_bytes()
        assert data.count(b"\x1b[<0;") == 2, data
        assert re.search(rb"\x1b\[<0;\d+;\d+M", data) and re.search(
            rb"\x1b\[<0;\d+;\d+m", data
        )
    else:
        data = _read_until(out, b"\x1b[B")
        assert b"\x1b[A" in data and b"\x1b[B" in data and b"\x1b[<" not in data
    quit_probe(page)
    assert errors == []
    ctx.close()


def test_old_history_goes_back_to_the_spacer_and_loads_again(page):
    """Past its cap the page gives its oldest chunks back to the spacer (the
    same height), and they load again from tmux when scrolled to."""
    page.evaluate(f"{ENGINE}.renderer.opts.maxRows = 600")
    run(page, "seq 1 3000")
    wait_text(page, "3000", "screen")
    page.wait_for_function(
        f"{ENGINE}.renderer.histRows <= 800 && {ENGINE}.renderer.spacerRows > 1500"
    )
    state = page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer;
            return {{rows: r.histRows, spacer: r.spacerRows, oldest: r.oldest, next: r.next,
                     height: r.scroller.scrollHeight}}; }}"""
    )
    dbg = page.evaluate(f"""() => {{ const r = {ENGINE}.renderer;
        return {{pend: [...document.querySelectorAll('.nt-pending')].map(p => p.style.height), pendings: r.pendings.map(p => [p.a, p.b]),
                 gaps: document.querySelectorAll('.nt-gap').length, first: r.firstHeld,
                 chunks: [...document.querySelectorAll('.nt-chunk')].map(c => [c._rows, c.querySelectorAll('.r').length, c._lines, c.children.length, c.style.containIntrinsicSize, c.offsetHeight]), spacer: r.spacerRows, live: r.live.length, nulls: r.live.filter(x => !x).length}}; }}""")
    assert page.evaluate(CONTIGUOUS), dbg
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
    pane_before = displayed_pane(page)
    tmux(tmux_env, "new-window")
    page.wait_for_function(f"{ENGINE}.pane !== {pane_before!r}", timeout=10000)
    ready(page)
    page.evaluate(f"window.__drain = {ENGINE}.drain; {ENGINE}.drain = () => {{}}")
    run(page, "seq 1 20000; echo FL''OOD-END")
    pane = displayed_pane(page)
    deadline = time.monotonic() + 20
    while "FLOOD-END" not in tmux(tmux_env, "capture-pane", "-p", "-t", pane):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    page.evaluate(f"{ENGINE}.drain = window.__drain; {ENGINE}.drain()")
    wait_text(page, "FLOOD-END", "screen")
    page.wait_for_function("document.querySelector('.nt-gap')")
    gap = page.evaluate(
        """() => { const gs = [...document.querySelectorAll('.nt-gap')];
            return {rows: gs.reduce((n, g) => n + +g.dataset.rows, 0), text: gs.map(g => g.textContent).join('')}; }"""
    )
    assert gap["rows"] > 1000 and gap["text"] == ""
    # Trimming down to a few rows removes the chunk with the marker.
    page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer; r.opts.maxRows = 50; r.opts.chunk = 50; }}"""
    )
    run(page, "seq 1 400")
    wait_text(page, "400", "screen")
    page.wait_for_function("!document.querySelector('.nt-gap')", timeout=10000)
    state = page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer;
            return {{rows: r.histRows, oldest: r.oldest, next: r.next, first: r.firstHeld,
                     spacer: r.spacerRows}}; }}"""
    )
    assert page.evaluate(CONTIGUOUS)
    assert state["spacer"] == max(0, state["oldest"] - state["first"])
    tmux(tmux_env, "kill-window")
    tmux(tmux_env, "set", "-g", "history-limit", "50000")


def test_a_selection_survives_the_output_that_scrolls_it(any_page, tmux_env):
    """Rows are never rebuilt or moved when output scrolls them into history,
    so a selection made on the live screen, across a hard line break and a soft
    wrap, keeps its nodes, its text and its copy while another writer floods the
    pane."""
    page = any_page
    cols = page.evaluate(f"{ENGINE}.renderer.cols")
    k = cols // 2 + 3
    line2 = "KEEP-2 " + "xy" * k + "TAIL"
    run(
        page,
        f"python3 -c \"print('KEEP' + '-1'); print('KEEP' + '-2 ' + 'xy' * {k} + 'TAIL'); print('KEEP' + '-3')\"",
    )
    wait_text(page, "KEEP-3", "screen")
    ready(page)
    before = page.evaluate(
        """() => {
            const r = window.MerlinNativeTerminalPage.engine.renderer;
            const ls = [...document.querySelectorAll('.nt-hist .l')];
            const a = ls.find(l => l.textContent === 'KEEP-1');
            const b = ls.find(l => l.textContent.startsWith('KEEP-2 '));
            const rows = b.querySelectorAll('.r');
            const start = a.querySelector('.r').firstChild;
            const end = rows[1].firstChild;
            document.activeElement.blur();
            const range = document.createRange();
            range.setStart(start, 2);
            range.setEnd(end, 5);
            const sel = getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            window.__ends = [start, end];
            return {rows: rows.length, onScreen: a.querySelector('.r')._n >= r.screenTop,
                    top: r.screenTop, text: sel.toString()};
        }"""
    )
    assert before["rows"] == 2 and before["onScreen"]
    tmux(tmux_env, "send-keys", "-t", displayed_pane(page), "seq 1 300", "Enter")
    wait_text(page, "300", "screen")
    page.wait_for_timeout(300)
    after = page.evaluate(
        """() => {
            const r = window.MerlinNativeTerminalPage.engine.renderer;
            const sel = getSelection();
            const range = sel.rangeCount ? sel.getRangeAt(0) : null;
            return {top: r.screenTop, count: sel.rangeCount, collapsed: sel.isCollapsed,
                    connected: window.__ends.every(n => n.isConnected),
                    same: !!range && range.startContainer === window.__ends[0]
                          && range.endContainer === window.__ends[1]
                          && range.startOffset === 2 && range.endOffset === 5,
                    text: sel.toString()};
        }"""
    )
    # The selected rows left the screen for history (the screen moved down by
    # more than its height), and nothing about the selection changed.
    assert after["top"] - before["top"] > page.evaluate(f"{ENGINE}.renderer.rows")
    assert after["count"] == 1 and not after["collapsed"]
    assert after["connected"] and after["same"]
    assert after["text"] == before["text"]
    expected = "EP-1\n" + line2[: cols + 5]
    page.evaluate(
        "window.__copied = null; window.addEventListener('copy',"
        " (e) => { window.__copied = e.clipboardData.getData('text/plain'); }, {once: true});"
    )
    page.keyboard.press("Control+c")
    page.wait_for_function("window.__copied !== null")
    assert page.evaluate("window.__copied") == expected
    if page.engine_name == "chromium":
        assert after["text"].rstrip("\n") == expected


# Draw frames one at a time: the page queues them, ``__step()`` draws the next.
STEPPER = f"""() => {{
    const e = {ENGINE};
    e.__drain = e.drain;
    e.drain = () => {{}};
    window.__step = () => {{
        if (!e.queue.length) return false;
        const rest = e.queue.slice(1);
        e.queue = e.queue.slice(0, 1);
        e.__drain();
        e.queue = rest.concat(e.queue);
        return true;
    }};
    window.__resume = () => {{ e.drain = e.__drain; e.drain(); }};
}}"""


def test_a_selection_survives_history_filling_in_above_it(page, tmux_env):
    """The screen moved down past history still on its way: its first row
    continues a long line whose first rows are a placeholder. A selection made
    from that row keeps its nodes, offsets and text when the placeholder fills
    and the line becomes one; trimming and loading back later leave the rows
    numbered one after the other from the first one in the DOM, at the exact
    height."""
    cols = page.evaluate(f"{ENGINE}.renderer.cols")
    rows = page.evaluate(f"{ENGINE}.renderer.rows")
    long_rows = rows + 3
    long = ("0123456789" * (cols * long_rows // 10 + 1))[: cols * long_rows - 3] + "END"
    page.evaluate(STEPPER)
    run(
        page,
        "python3 -c \"import sys; sys.stdout.write(''.join('%d\\n' % i for i in range(30000)));"
        f" print(('0123456789' * {cols * long_rows // 10 + 1})[:{cols * long_rows - 3}] + 'E' + 'ND');"
        " print('TAIL' + '-1'); print('TAIL' + '-2')\"",
    )
    pane = displayed_pane(page)
    deadline = time.monotonic() + 20
    while "TAIL-2" not in tmux(tmux_env, "capture-pane", "-p", "-t", pane):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    time.sleep(0.5)
    # Draw frames until the screen shows the end while the rows right above it
    # are still a placeholder.
    ready_to_select = f"""() => {{ const r = {ENGINE}.renderer;
        return r.pendings.length > 0 && r.screenText().includes('TAIL-2')
            && r.rowEl(r.screenTop - 1) == null; }}"""
    for _ in range(400):
        if page.evaluate(ready_to_select):
            break
        if not page.evaluate("window.__step()"):
            page.wait_for_timeout(50)
    assert page.evaluate(ready_to_select), page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer;
            return {{pend: r.pendings.map(p => [p.a, p.b]), top: r.screenTop, next: r.next,
                     screen: r.screenText().slice(-80), queue: {ENGINE}.queue.length}}; }}"""
    )
    before = page.evaluate(
        f"""() => {{
            const r = {ENGINE}.renderer;
            const top = r.rowEl(r.screenTop);
            const tail = [...document.querySelectorAll('.nt-hist .l')].find(l => l.textContent === 'TAIL-1');
            const start = top.firstChild, end = tail.querySelector('.r').firstChild;
            // A selection by hand takes the focus off the input field.
            document.activeElement.blur();
            const range = document.createRange();
            range.setStart(start, 2);
            range.setEnd(end, 4);
            const sel = getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            window.__ends = [start, end];
            return {{text: sel.toString(), lineRows: top.parentNode.querySelectorAll('.r').length}};
        }}"""
    )
    assert before["lineRows"] < long_rows  # the line's first rows are not here yet
    for _ in range(2000):
        if not page.evaluate("window.__step()"):
            if page.evaluate(f"{ENGINE}.renderer.pendings.length === 0"):
                break
            page.wait_for_timeout(50)
    page.evaluate("window.__resume()")
    page.wait_for_function(f"{ENGINE}.renderer.pendings.length === 0")
    after = page.evaluate(
        """() => {
            const sel = getSelection();
            const range = sel.rangeCount ? sel.getRangeAt(0) : null;
            const start = window.__ends[0];
            const line = start.parentNode.closest('.l');
            return {
                same: !!range && range.startContainer === window.__ends[0] && range.endContainer === window.__ends[1]
                      && range.startOffset === 2 && range.endOffset === 4,
                connected: window.__ends.every(n => n.isConnected),
                text: sel.toString(),
                line: line.textContent,
                lineRows: line.querySelectorAll('.r').length,
                index: [...line.querySelectorAll('.r')].indexOf(start.parentNode),
            };
        }"""
    )
    dbg = page.evaluate("""() => { const sel = getSelection(); const r = sel.rangeCount ? sel.getRangeAt(0) : null;
        return {n: sel.rangeCount, sc: r && (r.startContainer.nodeName + ':' + (r.startContainer.className || r.startContainer.textContent.slice(0, 20))), so: r && r.startOffset,
                ec: r && (r.endContainer.nodeName + ':' + (r.endContainer.className || r.endContainer.textContent.slice(0, 20))), eo: r && r.endOffset,
                c0: window.__ends[0].isConnected, c1: window.__ends[1].isConnected, p0: window.__ends[0].parentNode && window.__ends[0].parentNode.className}; }""")
    assert after["same"] and after["connected"], (dbg, after)
    assert after["text"] == before["text"]
    # One logical line again, its first rows filled in above the selection.
    assert after["line"] == long and after["lineRows"] == long_rows
    expected = long[after["index"] * cols + 2 :] + "\nTAIL"
    assert after["text"].rstrip("\n") == expected
    page.evaluate(
        "window.__copied = null; window.addEventListener('copy',"
        " (e) => { window.__copied = e.clipboardData.getData('text/plain'); }, {once: true});"
    )
    page.keyboard.press("Control+c")
    page.wait_for_function("window.__copied !== null")
    assert page.evaluate("window.__copied") == expected
    # Trimming, then loading everything back: rows numbered one after the
    # other from the first row in the DOM, at the exact height.
    page.evaluate(
        f"getSelection().removeAllRanges(); {ENGINE}.renderer.opts.maxRows = 400"
    )
    page.click("#nt-container")
    run(page, "seq 1 50")
    wait_text(page, "50", "screen")
    page.wait_for_function(f"{ENGINE}.renderer.rowCount <= 700")
    assert page.evaluate(CONTIGUOUS)
    page.evaluate(f"{ENGINE}.renderer.opts.maxRows = 100000")
    for _ in range(60):
        if page.evaluate(
            f"""() => {{ const r = {ENGINE}.renderer, s = r.scroller;
                s.scrollTop = Math.max(0, r.spacerHeight() - s.clientHeight);
                return r.spacerRows === 0; }}"""
        ):
            break
        page.wait_for_timeout(150)
    final = page.evaluate(
        f"""() => {{ const r = {ENGINE}.renderer;
            return {{spacer: r.spacerRows, height: r.content.offsetHeight, rows: r.rowCount, rowH: r.rowH,
                     line: [...document.querySelectorAll('.nt-hist .l')].some(l => l.textContent === {long!r})}}; }}"""
    )
    assert final["spacer"] == 0 and final["line"]
    assert final["height"] == final["rows"] * final["rowH"]
    assert page.evaluate(CONTIGUOUS)


# A renderer of its own on the page, fed rows directly: rows 0..2 of a 10-column
# pane, and the text a copy of all of them gives (lines joined by newlines).
REGROUP = """([flagsSeq, selectFrom]) => {
    const host = document.createElement('div');
    host.style.cssText = 'position:fixed;left:0;top:0;width:400px;height:200px;z-index:99';
    document.body.append(host);
    const r = new MerlinNativeRenderer(host);
    r.reset(10, 3);
    const rows = ['aaaaaaaaaa', 'bbb', 'ccc'];
    const apply = (flags) => r.applyScreen(0, rows, rows, flags, null, false);
    const lines = () => [...host.querySelectorAll('.l')].map(l => l.textContent);
    const copy = () => lines().join('\\n');
    apply(flagsSeq[0]);
    // Select across rows 0 and 1.
    const a = r.rowEl(0).firstChild, b = r.rowEl(1).firstChild;
    const range = document.createRange();
    range.setStart(a, 2);
    range.setEnd(b, 2);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    const seen = [];
    for (const flags of flagsSeq.slice(1)) {
        apply(flags);
        const g = getSelection().getRangeAt(0);
        seen.push({lines: lines(), kept: g.startContainer === a && g.endContainer === b
                   && g.startOffset === 2 && g.endOffset === 2, deferred: r.deferred.size});
    }
    getSelection().removeAllRanges();
    apply(flagsSeq[flagsSeq.length - 1]);  // the next update, selection gone
    const out = {seen, lines: lines(), copy: copy(), deferred: r.deferred.size};
    host.remove();
    return out;
}"""


@pytest.mark.parametrize(
    "flags, final_lines",
    [
        # 0 -> 1 -> 0 while selected: two lines in the end, not merged.
        (["000", "100", "000"], ["aaaaaaaaaa", "bbb", "ccc"]),
        # 1 -> 0 -> 1 while selected: one line in the end, not split.
        (["100", "000", "100"], ["aaaaaaaaaabbb", "ccc"]),
        # 0 -> 1 while selected: merged once the selection is gone.
        (["000", "100"], ["aaaaaaaaaabbb", "ccc"]),
        # 1 -> 0 while selected: split once the selection is gone.
        (["100", "000"], ["aaaaaaaaaa", "bbb", "ccc"]),
    ],
)
def test_deferred_regrouping_follows_the_latest_wrap_flags(page, flags, final_lines):
    """A regrouping a selection blocks waits, keeps the selection, and is
    reconciled with the wrap flags as they are when the selection goes."""
    out = page.evaluate(REGROUP, [flags, 0])
    assert all(s["kept"] for s in out["seen"]), out
    assert out["lines"] == final_lines, out
    assert out["copy"] == "\n".join(final_lines)
    assert out["deferred"] == 0
