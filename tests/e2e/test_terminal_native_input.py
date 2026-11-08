"""E2E tests for the native terminal's input (``/terminal/native``).

The text field with its sentinel and the diff model, the physical keyboard
encoded as tmux key names (tmux turns them into the bytes of the pane's
modes), the toolbar with its sticky modifiers and repeat buttons, the focus
rules, the copy and paste shortcuts. A recorder program in raw mode writes
every byte the pane reads, so each test checks the exact bytes.

The iOS keyboard itself (held delete accelerating, the space-bar trackpad,
dictation) cannot be driven by Playwright: the event sequences it produces
are replayed in tests/js/native-input.test.js, and the real thing is on the
founder's iPhone checklist (the epic's journal).

Run: uv run scripts.py test-e2e   (or pytest tests/e2e/test_terminal_native_input.py)
Requires: chromium + tmux.
"""

import shutil
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_terminal_native import (  # noqa: E402
    ENGINE,
    _open,
    browser,  # noqa: F401  (fixture)
    firefox,  # noqa: F401  (fixture)
    playwright_driver,  # noqa: F401  (fixture)
    ready,
    run,
    tmux,
    wait_text,
)

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="web terminal needs tmux"
)

SENTINEL = "\u00a0" * 4
QUIT = b"\x1d"  # Ctrl+]: the recorder stops

RECORDER = textwrap.dedent(
    """
    import os, sys, termios, tty
    out, mode, token = sys.argv[1], sys.argv[2], sys.argv[3]
    saved = termios.tcgetattr(0)
    tty.setraw(0)  # flushes what was typed before: wait for READY
    pre = {"app": "\\033[?1h\\033=", "paste": "\\033[?2004h"}.get(mode, "")
    os.write(1, (pre + "REC " + token + " READY\\r\\n").encode())
    data = b""
    try:
        while b"\\x1d" not in data:
            data += os.read(0, 4096)
            open(out, "wb").write(data)
    finally:
        os.write(1, b"\\033[?1l\\033>\\033[?2004l")
        termios.tcsetattr(0, termios.TCSADRAIN, saved)
    """
)


@pytest.fixture
def page(browser, server):  # noqa: F811
    ctx, page, errors = _open(browser, server)
    page.click("#nt-container")
    ready(page)
    run(page, "clear")
    ready(page)
    yield page
    assert errors == []
    ctx.close()


@pytest.fixture(params=["chromium", "firefox"])
def any_page(request, server):
    """The text field and the keys in both desktop engines this machine runs
    (WebKit, the engine of iOS, is checked in the throwaway instance's
    container: the epic's spike/phase4/)."""
    b = request.getfixturevalue("browser" if request.param == "chromium" else "firefox")
    ctx, page, errors = _open(b, server)
    page.click("#nt-container")
    ready(page)
    run(page, "clear")
    ready(page)
    yield page
    assert errors == []
    ctx.close()


def record(page, tmp_path: Path, mode: str = "plain") -> Path:
    script = tmp_path / "rec.py"
    script.write_text(RECORDER)
    out = tmp_path / f"{mode}.bin"
    # A marker of its own: one left on the screen by an earlier recorder
    # must not pass for this one (keys typed before it starts are lost).
    token = f"{mode.upper()}{time.monotonic_ns() % 10**9}"
    run(page, f"python3 {script} {out} {mode} {token}")
    wait_text(page, f"REC {token} READY", "screen")
    return out


def until(path: Path, needle: bytes, timeout: float = 8.0) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = path.read_bytes() if path.exists() else b""
        if needle in data:
            return data
        time.sleep(0.03)
    return path.read_bytes() if path.exists() else b""


def finish(page, path: Path) -> bytes:
    """Every byte the pane read until now (the recorder stops on QUIT)."""
    page.evaluate(f"{ENGINE}.input('\\x1d')")
    data = until(path, QUIT)
    assert data.endswith(QUIT), data
    ready(page)
    return data[: -len(QUIT)]


def field_value(page) -> str:
    return page.evaluate("document.getElementById('nt-input').value")


# -- the text field --------------------------------------------------------------


def test_typed_text_reaches_the_program_exactly_once(any_page, tmp_path):
    """Words that look like tmux keys, tmux's separators, UTF-8: as typed."""
    page = any_page
    out = record(page, tmp_path)
    text = "Enter C-c Space ; \\ # 'x' é 日本 🎉"
    page.keyboard.type(text)
    page.keyboard.press("Enter")
    assert finish(page, out) == text.encode() + b"\r"
    # The sentinel never reached the pane, and the field is back to it.
    assert field_value(page) == SENTINEL * 2


def test_a_rewrite_of_the_typed_text_erases_back_to_the_change(any_page, tmp_path):
    """What iOS does for dictation, autocorrect and predictions: it replaces
    text it already put in the field. Driven here with the browser's own
    editing (a selection replaced by insertText), as real input events."""
    page = any_page
    out = record(page, tmp_path)
    page.keyboard.type("bonne nuit")
    page.evaluate(
        """() => {
            const f = document.getElementById('nt-input');
            f.setSelectionRange(4, 14);  // the typed text, inside the sentinels
            document.execCommand('insertText', false, 'Bonne nuit');
        }"""
    )
    # A curly apostrophe (iOS smart punctuation) is a plain one.
    page.evaluate("document.execCommand('insertText', false, ' c’est')")
    page.keyboard.press("Enter")
    data = finish(page, out)
    assert data == b"bonne nuit" + b"\x7f" * 10 + b"Bonne nuit c'est\r"


def test_backspace_deletes_typed_text_then_keeps_going(any_page, tmp_path):
    page = any_page
    out = record(page, tmp_path)
    page.keyboard.type("ab")
    for _ in range(5):
        page.keyboard.press("Backspace")
    data = finish(page, out)
    assert data == b"ab" + b"\x7f" * 5
    assert field_value(page) == SENTINEL * 2


def test_an_ime_composition_commits_once(page, tmp_path):
    out = record(page, tmp_path)
    cdp = page.context.new_cdp_session(page)
    for text in ("k", "か", "かん"):
        cdp.send(
            "Input.imeSetComposition",
            {"text": text, "selectionStart": len(text), "selectionEnd": len(text)},
        )
    # The composition shows at the cursor and nothing is sent yet.
    page.wait_for_function(
        "() => { const p = document.querySelector('.nt-preedit');"
        " return p && !p.hidden && p.textContent === 'かん'; }"
    )
    time_sent = out.read_bytes() if out.exists() else b""
    assert time_sent == b""
    cdp.send("Input.insertText", {"text": "漢"})
    page.keyboard.type("x")
    data = finish(page, out)
    assert data == "漢x".encode()
    assert page.evaluate("document.querySelector('.nt-preedit').hidden")


def test_a_paste_in_the_field_goes_through_the_paste_path(page, tmp_path):
    """Ctrl+V is the browser's paste (text and images, no prompt): the
    program that asked for bracketed paste gets the markers."""
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    out = record(page, tmp_path, "paste")
    page.evaluate("navigator.clipboard.writeText('one; two \\\\ C-c\\nline2')")
    page.keyboard.press("Control+v")
    data = until(out, b"\x1b[201~")
    assert data == b"\x1b[200~one; two \\ C-c\nline2\x1b[201~"
    finish(page, out)


# -- the physical keyboard ----------------------------------------------------------

KEYS = [
    # (Playwright key, normal, application cursor and keypad)
    ("ArrowUp", b"\x1b[A", b"\x1bOA"),
    ("ArrowLeft", b"\x1b[D", b"\x1bOD"),
    ("Control+ArrowLeft", b"\x1b[1;5D", b"\x1b[1;5D"),
    ("Shift+ArrowRight", b"\x1b[1;2C", b"\x1b[1;2C"),
    ("Alt+ArrowUp", b"\x1b[1;3A", b"\x1b[1;3A"),
    ("Home", b"\x1b[1~", b"\x1b[1~"),
    ("End", b"\x1b[4~", b"\x1b[4~"),
    ("PageUp", b"\x1b[5~", b"\x1b[5~"),
    ("Delete", b"\x1b[3~", b"\x1b[3~"),
    ("Insert", b"\x1b[2~", b"\x1b[2~"),
    ("F1", b"\x1bOP", b"\x1bOP"),
    ("F12", b"\x1b[24~", b"\x1b[24~"),
    ("Tab", b"\t", b"\t"),
    ("Shift+Tab", b"\x1b[Z", b"\x1b[Z"),
    ("Escape", b"\x1b", b"\x1b"),
    ("Control+a", b"\x01", b"\x01"),
    ("Control+c", b"\x03", b"\x03"),
    ("Control+Shift+A", b"\x01", b"\x01"),
    ("Alt+b", b"\x1bb", b"\x1bb"),
    ("Alt+Shift+B", b"\x1bB", b"\x1bB"),
    ("Alt+Backspace", b"\x1b\x7f", b"\x1b\x7f"),
    ("Control+Backspace", b"\x08", b"\x08"),
    ("Control+Space", b"\x00", b"\x00"),
    ("Control+BracketLeft", b"\x1b", b"\x1b"),
    ("Backspace", b"\x7f", b"\x7f"),
    ("Enter", b"\r", b"\r"),
]


@pytest.mark.parametrize("mode", ["plain", "app"])
def test_physical_keys_reach_the_program_in_its_modes(any_page, tmp_path, mode):
    page = any_page
    out = record(page, tmp_path, mode)
    col = 1 if mode == "plain" else 2
    expected = b""
    for key, *want in KEYS:
        page.keyboard.press(key)
        expected += want[col - 1]
        # One at a time: a key and its bytes, in order.
        assert until(out, expected).endswith(expected), (key, out.read_bytes())
    assert finish(page, out) == expected


# -- the toolbar ----------------------------------------------------------------------


def tool(page, label: str) -> None:
    page.locator("#nt-toolbar .tk", has_text=label).first.click()


def test_toolbar_keys_and_sticky_modifiers(page, tmp_path):
    out = record(page, tmp_path, "app")
    tool(page, "Esc")
    tool(page, "Tab")
    tool(page, "⇧Tab")
    tool(page, "↵")
    # A sticky modifier lights up, applies to the next key from the
    # keyboard, then clears.
    page.locator("#nt-toolbar [data-mod=ctrl]").click()
    assert page.locator("#nt-toolbar [data-mod=ctrl]").evaluate(
        "b => b.classList.contains('active')"
    )
    page.keyboard.press("c")
    assert not page.locator("#nt-toolbar [data-mod=ctrl]").evaluate(
        "b => b.classList.contains('active')"
    )
    # ... or to the next toolbar key: Alt then a toolbar arrow.
    page.locator("#nt-toolbar [data-mod=alt]").click()
    page.locator("#nt-toolbar [data-key=ArrowLeft]").click()
    page.locator("#nt-toolbar [data-mod=shift]").click()
    tool(page, "Tab")
    # The buttons never took the focus from the field.
    assert page.evaluate("document.activeElement.id") == "nt-input"
    data = finish(page, out)
    assert data == b"\x1b\t\x1b[Z\r\x03\x1b[1;3D\x1b[Z"


def test_a_held_toolbar_arrow_repeats(page, tmp_path):
    out = record(page, tmp_path)
    # The toolbar row scrolls when the terminal column is narrow (the docked
    # Sessions panel): the arrow on screen first.
    arrow = page.locator("#nt-toolbar [data-key=ArrowUp]")
    arrow.scroll_into_view_if_needed()
    box = arrow.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(1000)
    page.mouse.up()
    page.wait_for_timeout(300)
    data = finish(page, out)
    n = data.count(b"\x1b[A")
    # Once at once, then every 60 ms after 400 ms: about 11 in a second.
    assert data == b"\x1b[A" * n and 6 <= n <= 14, data


def test_toolbar_window_keys(page, tmux_env):
    session = page.evaluate("window.MerlinNativeTerminalPage.current().name")
    windows = lambda: tmux(
        tmux_env, "list-windows", "-t", session, "-F", "#{window_id}"
    ).split()  # noqa: E731
    before = windows()
    tool(page, "+")  # the Sessions panel's create-and-switch path
    deadline = time.monotonic() + 8
    while len(windows()) != len(before) + 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    after = windows()
    assert len(after) == len(before) + 1
    page.wait_for_function(
        "id => window.MerlinNativeTerminalPage.current().window_id === id",
        arg=after[-1],
    )
    tool(page, "◀")
    page.wait_for_function(
        "id => window.MerlinNativeTerminalPage.current().window_id === id",
        arg=after[-2],
    )
    tool(page, "▶")
    page.wait_for_function(
        "id => window.MerlinNativeTerminalPage.current().window_id === id",
        arg=after[-1],
    )
    tool(page, "✕")
    while len(windows()) != len(before) and time.monotonic() < deadline + 8:
        time.sleep(0.05)
    assert windows() == before


# -- focus, copy -------------------------------------------------------------------------


def test_after_a_mouse_selection_ctrl_c_copies_and_typing_goes_on(page, tmp_path):
    run(page, "echo SELECT''ME")
    wait_text(page, "SELECTME", ".nt-content")
    out = record(page, tmp_path)
    line = page.locator(".nt-content .l", has_text="SELECTME").last
    box = line.bounding_box()
    page.mouse.move(box["x"] + 1, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + 200, box["y"] + box["height"] / 2, steps=5)
    page.mouse.up()
    assert page.evaluate("getSelection().toString()").startswith("SELECTME")
    assert page.evaluate("document.activeElement.id") != "nt-input"
    page.evaluate(
        "window.__copied = null; window.addEventListener('copy', e =>"
        " { setTimeout(() => {}, 0); window.__copied = e.clipboardData.getData('text/plain'); }, {once: true})"
    )
    page.keyboard.press("Control+c")
    page.wait_for_function("window.__copied !== null")
    assert page.evaluate("window.__copied").startswith("SELECTME")
    # Typing goes to the terminal, and the field has the focus again.
    page.keyboard.type("xy")
    assert page.evaluate("document.activeElement.id") == "nt-input"
    page.keyboard.press("Control+c")  # no selection now: ^C
    assert finish(page, out) == b"xy\x03"


def test_ctrl_shift_c_copies_the_selection(page):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    run(page, "echo COPY''THIS")
    wait_text(page, "COPYTHIS", ".nt-content")
    # A real drag, as a user selects (it takes the focus off the field).
    box = page.evaluate(
        """() => {
            const l = [...document.querySelectorAll('.nt-content .l')].filter(x => x.textContent.startsWith('COPYTHIS')).pop();
            const r = document.createRange();
            r.selectNodeContents(l);
            const b = r.getBoundingClientRect();
            return {x: b.left, y: b.top + b.height / 2, w: b.width};
        }"""
    )
    page.mouse.move(box["x"] + 1, box["y"])
    page.mouse.down()
    page.mouse.move(box["x"] + box["w"] - 1, box["y"], steps=5)
    page.mouse.up()
    assert page.evaluate("getSelection().toString().trim()") == "COPYTHIS"
    page.keyboard.press("Control+Shift+C")
    # Polled here: wait_for_function takes a returned promise for a truthy value.
    deadline = time.monotonic() + 5
    while (
        page.evaluate("navigator.clipboard.readText()") != "COPYTHIS"
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(50)
    assert page.evaluate("navigator.clipboard.readText()") == "COPYTHIS"


def test_a_toolbar_key_resets_the_field_like_a_physical_key(page, tmp_path):
    """What was typed no longer sits before the terminal's cursor once a
    toolbar key moved it: the field starts afresh, so a later rewrite or
    delete is about the new position."""
    out = record(page, tmp_path)
    page.keyboard.type("abc")
    page.locator("#nt-toolbar [data-key=ArrowLeft]").click()
    page.wait_for_function(
        "s => document.getElementById('nt-input').value === s", arg=SENTINEL * 2
    )
    page.keyboard.press("Backspace")
    tool(page, "↵")
    assert field_value(page) == SENTINEL * 2
    assert finish(page, out) == b"abc\x1b[D\x7f\r"


def test_a_toolbar_key_during_a_composition_comes_after_it(page, tmp_path):
    """The composition commits where it was typed (the browser commits it on
    the blur the page gives it), then the key."""
    out = record(page, tmp_path)
    page.keyboard.type("ab")
    cdp = page.context.new_cdp_session(page)
    cdp.send(
        "Input.imeSetComposition",
        {"text": "か", "selectionStart": 1, "selectionEnd": 1},
    )
    page.wait_for_function("window.MerlinNativeTerminalPage.input.composing")
    page.locator("#nt-toolbar [data-key=ArrowLeft]").click()
    page.wait_for_function("!window.MerlinNativeTerminalPage.input.composing")
    assert field_value(page) == SENTINEL * 2
    assert page.evaluate("document.activeElement.id") == "nt-input"
    page.keyboard.type("X")
    assert finish(page, out) == "abか\x1b[DX".encode()


def test_another_pane_of_the_window_gets_nothing_of_the_field(page, tmp_path, tmux_env):
    """Text typed for one pane stays out of the next: the field starts afresh
    when the displayed pane changes, so a later rewrite or delete cannot erase
    in the new pane."""
    page.keyboard.type("abc")
    first = page.evaluate(f"{ENGINE}.pane")
    script = tmp_path / "rec.py"
    script.write_text(RECORDER)
    out = tmp_path / "split.bin"
    token = f"SPLIT{time.monotonic_ns() % 10**9}"
    tmux(tmux_env, "split-window", "-t", first, f"python3 {script} {out} plain {token}")
    page.wait_for_function(f"p => {ENGINE}.pane !== p", arg=first)
    wait_text(page, f"REC {token} READY", "screen")
    assert field_value(page) == SENTINEL * 2
    # What a rewrite of "abc" would be now: the new pane gets only the text.
    page.evaluate("document.execCommand('insertText', false, 'ABC')")
    page.keyboard.press("Backspace")
    page.evaluate(f"{ENGINE}.input('\\x1d')")
    data = until(out, QUIT)
    assert data == b"ABC\x7f" + QUIT
    # Back on the first pane (the recorder's pane closed): clear its line.
    page.wait_for_function(f"p => {ENGINE}.pane === p", arg=first)
    page.evaluate(f"{ENGINE}.input('\\x15')")
    ready(page)


# -- the field's own paste surface on a touch screen -----------------------------------

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


PHONE = {
    "viewport": {"width": 390, "height": 664},
    "is_mobile": True,
    "has_touch": True,
    "device_scale_factor": 2,
}


def _long_press(page, cdp, x: float, y: float) -> None:
    """A real touch held long enough to be a long press."""
    point = [{"x": x, "y": y}]
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": point})
    page.wait_for_timeout(800)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(200)


def test_on_a_phone_a_long_press_reaches_the_field_and_sends_nothing(
    browser,  # noqa: F811
    server,
    tmp_path,
):
    """The system's Paste menu needs a real field under the finger: the field
    lies over the Paste button, the terminal's text stays selectable, and the
    caret a long press places never becomes arrows. The iOS menu itself is
    on the founder's device checklist; its paste event is what is sent here."""
    ctx, page, errors = _open(
        browser,
        server,
        viewport={"width": 390, "height": 664},
        is_mobile=True,
        has_touch=True,
        device_scale_factor=2,
    )
    cdp = ctx.new_cdp_session(page)
    hit = page.evaluate(
        """() => {
            const b = document.getElementById('paste-btn').getBoundingClientRect();
            const x = b.left + b.width / 2, y = b.top + b.height / 2;
            const field = document.getElementById('nt-input');
            const f = field.getBoundingClientRect();
            const term = document.getElementById('nt-container').getBoundingClientRect();
            return {x, y, w: b.width, h: b.height, right: b.right, bottom: b.bottom,
                field: [f.left, f.top, f.width, f.height],
                button: [b.left, b.top, b.width, b.height],
                hit: document.elementFromPoint(x, y).id,
                select: getComputedStyle(field).userSelect,
                label: document.getElementById('paste-btn').textContent.trim(),
                output: !!document.elementFromPoint(term.left + term.width / 2, term.top + term.height / 2)
                    .closest('#nt-container')}; }"""
    )
    assert hit["hit"] == "nt-input"
    assert hit["field"] == pytest.approx(hit["button"], abs=1)
    assert hit["select"] == "text"
    assert hit["label"] == "Paste"
    # On screen whatever the toolbar's scroll, and the terminal's text is still
    # the terminal's to select.
    assert hit["right"] <= 390
    assert hit["output"]

    page.tap("#nt-container")
    ready(page)
    out = record(page, tmp_path, "paste")

    # A long press, then typing: only the key, no arrow for the caret.
    _long_press(page, cdp, hit["x"], hit["y"])
    assert page.evaluate("document.activeElement.id") == "nt-input"
    page.keyboard.type("a")
    until(out, b"a")

    # A long press, then the Paste of the system's menu: bracketed, once.
    _long_press(page, cdp, hit["x"], hit["y"])
    page.evaluate(
        """() => { const dt = new DataTransfer(); dt.setData('text/plain', 'PASTED; C-c');
            document.getElementById('nt-input').dispatchEvent(
                new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }"""
    )
    until(out, b"\x1b[201~")

    # A long press, then an image from the same menu: its path is typed.
    _long_press(page, cdp, hit["x"], hit["y"])
    page.evaluate(
        """(bytes) => { const dt = new DataTransfer();
            dt.items.add(new File([new Uint8Array(bytes)], 'held.png', {type: 'image/png'}));
            document.getElementById('nt-input').dispatchEvent(
                new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }""",
        list(PNG_BYTES),
    )
    until(out, b"held.png")
    page.keyboard.type("b")
    until(out, b"held.pngb")

    # A long press, then a composition with no key before it: where typing goes.
    _long_press(page, cdp, hit["x"], hit["y"])
    cdp.send(
        "Input.imeSetComposition",
        {"text": "か", "selectionStart": 1, "selectionEnd": 1},
    )
    cdp.send("Input.insertText", {"text": "漢"})
    until(out, "漢".encode())
    data = finish(page, out)
    assert data.endswith("-held.pngb漢".encode()), data
    data = data[: -len("漢".encode())]
    head = b"a\x1b[200~PASTED; C-c\x1b[201~/tmp/merlin-clipboard/"
    assert data.startswith(head), data
    assert data.endswith(b"-held.pngb"), data
    assert b"\x1b[C" not in data and b"\x1b[D" not in data, data
    path = data[len(b"a\x1b[200~PASTED; C-c\x1b[201~") : -1].decode()
    assert Path(path).read_bytes() == PNG_BYTES
    assert errors == []
    ctx.close()


def test_on_a_phone_a_tap_on_paste_reads_the_clipboard(
    browser,  # noqa: F811
    server,
    tmp_path,
):
    ctx, page, errors = _open(browser, server, **PHONE)
    page.tap("#nt-container")
    ready(page)
    out = record(page, tmp_path, "paste")
    page.evaluate(
        "navigator.clipboard.readText = async () => 'TAPPED';"
        "navigator.clipboard.read = undefined;"
    )
    page.tap("#nt-input")  # over the Paste button
    until(out, b"\x1b[201~")
    assert finish(page, out) == b"\x1b[200~TAPPED\x1b[201~"
    assert errors == []
    ctx.close()


def _at_bottom(page) -> bool:
    return page.evaluate(
        f"""() => {{ const s = {ENGINE}.scroller;
            return s.scrollTop + s.clientHeight >= s.scrollHeight - 4; }}"""
    )


def test_a_touch_on_a_row_output_redraws_does_not_stop_the_following(
    browser,  # noqa: F811
    server,
):
    """The end of the touch goes to a node no longer in the document."""
    ctx, page, errors = _open(browser, server, **PHONE)
    cdp = ctx.new_cdp_session(page)
    page.tap("#nt-container")
    ready(page)
    run(
        page,
        "while :; do printf '\\r\\033[31mtick %s\\033[0m' $RANDOM; sleep 0.05; done",
    )
    wait_text(page, "tick", "screen")
    page.wait_for_function(
        """[...document.querySelectorAll('.nt-hist .r span, .nt-alt .r span')]
            .some(e => e.textContent.includes('tick'))""",
        timeout=5000,
    )
    point = page.evaluate(
        """() => { const spans = [...document.querySelectorAll('.nt-hist .r span, .nt-alt .r span')]
                .filter(e => e.textContent.includes('tick'));
            const b = spans[spans.length - 1].getBoundingClientRect();
            window.__touched = document.elementFromPoint(b.left + 3, b.top + 3);
            return {x: b.left + 3, y: b.top + 3}; }"""
    )
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
    page.wait_for_function("!window.__touched.isConnected", timeout=5000)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.evaluate(f"{ENGINE}.input('\\x03')")
    ready(page)
    assert page.evaluate(f"{ENGINE}.renderer.touching") is False
    page.tap("#nt-toolbar [data-key='Enter']")
    ready(page)
    run(page, "seq 1 300; echo FOLLOWED")
    wait_text(page, "FOLLOWED", "screen")
    page.wait_for_function(
        f"""() => {{ const s = {ENGINE}.scroller;
            return s.scrollTop + s.clientHeight >= s.scrollHeight - 4; }}""",
        timeout=5000,
    )
    assert errors == []
    ctx.close()


def test_the_page_last_used_sizes_the_window_and_a_shortened_view_stays_at_the_bottom(
    browser,  # noqa: F811
    server,
    tmux_env,
):
    """As with attached clients: the last one the user touched. The phone's
    keyboard shortens its view with no frame to come, while the desktop keeps
    the window's size."""
    phone, p_page, p_errors = _open(browser, server, **PHONE)
    desk, d_page, d_errors = _open(browser, server)
    width = lambda: int(  # noqa: E731
        tmux(
            tmux_env,
            "display-message",
            "-p",
            "-t",
            p_page.evaluate("window.MerlinTerminal.currentWindow()"),
            "#{window_width}",
        )
    )
    cols = lambda pg: pg.evaluate(f"{ENGINE}.size.cols")  # noqa: E731

    def until_width(want: int) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and width() != want:
            time.sleep(0.05)
        assert width() == want

    p_page.tap("#nt-container")
    ready(p_page)
    run(p_page, "seq 1 300")
    wait_text(p_page, "300", "screen")
    d_page.click("#nt-container")
    until_width(cols(d_page))
    assert cols(d_page) > cols(p_page)

    p_page.wait_for_timeout(1100)
    p_page.tap("#nt-container")
    until_width(cols(p_page))

    d_page.wait_for_timeout(1100)
    box = d_page.locator("#nt-container").bounding_box()
    d_page.mouse.move(box["x"] + 50, box["y"] + 50)
    d_page.mouse.wheel(0, -30)
    until_width(cols(d_page))

    # The window stays the desktop's: nothing is drawn on the phone.
    p_page.wait_for_function(f"{ENGINE}.renderer.cols === {cols(d_page)}", timeout=8000)
    assert _at_bottom(p_page)
    p_page.set_viewport_size({"width": 390, "height": 400})
    p_page.wait_for_timeout(300)
    assert _at_bottom(p_page)
    assert p_errors == [] and d_errors == []
    desk.close()
    phone.close()


def _view(page) -> dict:
    """Where the cursor and the last row are, against the visible scroller."""
    return page.evaluate(
        f"""() => {{
            const e = {ENGINE}, r = e.renderer, box = e.scroller.getBoundingClientRect();
            const all = r.hist.querySelectorAll('.r'), last = all[all.length - 1];
            const k = r.cursor.getBoundingClientRect();
            return {{top: box.top, bottom: box.bottom, cursorTop: k.top, cursorBottom: k.bottom,
                     rows: r.rows, screenTop: r.screenTop, next: r.next, live: r.live.length,
                     lastRow: last._n, count: all.length, rowCount: r.rowCount,
                     text: r.screenText()}};
        }}"""
    )


def test_a_shorter_screen_leaves_no_old_rows_below_it(
    browser,  # noqa: F811
    server,
):
    """The phone's keyboard makes the screen shorter: what is typed then shows
    at the bottom of the view, not above rows the screen no longer has."""
    ctx, page, errors = _open(browser, server, **PHONE)
    page.tap("#nt-container")
    ready(page)
    run(page, "seq 1 300")
    wait_text(page, "300", "screen")
    tall = page.evaluate(f"{ENGINE}.renderer.rows")

    page.set_viewport_size({"width": 390, "height": 340})
    page.wait_for_function(f"{ENGINE}.renderer.rows < {tall}", timeout=8000)
    page.evaluate(f"{ENGINE}.input('typed-with-keyboard')")
    wait_text(page, "typed-with-keyboard", "screen")
    page.wait_for_timeout(200)
    view = _view(page)
    assert view["lastRow"] == view["screenTop"] + view["rows"] - 1
    assert view["count"] == view["rowCount"]
    assert view["top"] <= view["cursorTop"] and view["cursorBottom"] <= view["bottom"]

    page.set_viewport_size({"width": 390, "height": 664})
    page.wait_for_function(f"{ENGINE}.renderer.rows === {tall}", timeout=8000)
    page.wait_for_timeout(300)
    view = _view(page)
    assert "typed-with-keyboard" in view["text"]
    assert view["lastRow"] == view["screenTop"] + view["rows"] - 1
    assert view["count"] == view["rowCount"]
    assert view["top"] <= view["cursorTop"] and view["cursorBottom"] <= view["bottom"]
    assert errors == []
    page.evaluate(f"{ENGINE}.input('\\x15')")  # the prompt, clean for the next test
    wait_gone = f"!{ENGINE}.renderer.screenText().includes('typed-with-keyboard')"
    page.wait_for_function(wait_gone, timeout=5000)
    ctx.close()


def _keyboard(page, height: int | None) -> None:
    """The software keyboard as the page sees it: a shorter visual viewport."""
    page.evaluate(
        """(h) => { const vv = window.visualViewport;
            if (h === null) delete vv.height;
            else Object.defineProperty(vv, 'height', {configurable: true, get: () => h});
            vv.dispatchEvent(new Event('resize')); }""",
        height,
    )
    page.wait_for_timeout(250)


def _view_bottom(page) -> float:
    return page.evaluate(
        f"(() => {{ const s = {ENGINE}.scroller; return s.scrollTop + s.clientHeight; }})()"
    )


def test_the_keyboard_shortens_the_view_and_leaves_the_window_its_height(
    browser,  # noqa: F811
    server,
):
    """What was at the bottom of the view is still there, just above the
    keyboard, and the program is not resized."""
    ctx, page, errors = _open(browser, server, **PHONE)
    page.tap("#nt-container")
    ready(page)
    run(page, "seq 1 300")
    wait_text(page, "300", "screen")
    rows = page.evaluate(f"{ENGINE}.size.rows")

    # Following the output: the live rows, what is typed included.
    _keyboard(page, 340)
    assert page.evaluate(f"{ENGINE}.size.rows") == rows
    page.evaluate(f"{ENGINE}.input('typed-with-keyboard')")
    wait_text(page, "typed-with-keyboard", "screen")
    page.wait_for_timeout(200)
    view = _view(page)
    assert view["top"] <= view["cursorTop"] and view["cursorBottom"] <= view["bottom"]
    page.evaluate(f"{ENGINE}.input('\\x15')")
    page.wait_for_function(
        f"!{ENGINE}.renderer.screenText().includes('typed-with-keyboard')", timeout=5000
    )
    _keyboard(page, None)

    # Read a little higher up: the same row stays at the bottom, both ways.
    page.evaluate(f"{ENGINE}.scroller.scrollTop -= 70")
    page.wait_for_function(f"{ENGINE}.renderer.stuck === false", timeout=5000)
    before = _view_bottom(page)
    _keyboard(page, 340)
    assert _view_bottom(page) == before
    assert page.evaluate(f"{ENGINE}.renderer.stuck") is False
    _keyboard(page, None)
    assert _view_bottom(page) == before
    assert page.evaluate(f"{ENGINE}.size.rows") == rows
    assert errors == []
    ctx.close()


def test_with_the_keyboard_up_the_blank_rows_of_a_new_shell_are_not_text(
    browser,  # noqa: F811
    server,
):
    """The prompt at the top of the screen stays at the top of the view."""
    ctx, page, errors = _open(browser, server, **PHONE)
    page.tap("#nt-container")
    ready(page)
    run(page, "seq 1 300; clear")
    page.wait_for_function(f"{ENGINE}.state.cursor.y < 3", timeout=8000)
    _keyboard(page, 340)
    page.wait_for_timeout(200)
    screen_top = f"""(() => {{ const e = {ENGINE}, r = e.renderer;
        return r.rowEl(r.screenTop).getBoundingClientRect().top
            - e.scroller.getBoundingClientRect().top; }})()"""
    assert abs(page.evaluate(screen_top)) < 2
    view = _view(page)
    assert view["top"] <= view["cursorTop"] and view["cursorBottom"] <= view["bottom"]
    assert page.evaluate(f"{ENGINE}.renderer.stuck") is True

    # Typing keeps it there, and once the screen is full the end shows.
    page.evaluate(f"{ENGINE}.input('x')")
    page.wait_for_function(
        f"{ENGINE}.renderer.screenText().includes('x')", timeout=5000
    )
    assert abs(page.evaluate(screen_top)) < 2
    page.evaluate(f"{ENGINE}.input('\\x15')")
    ready(page)
    run(page, "seq 1 300")
    wait_text(page, "300", "screen")
    page.wait_for_timeout(200)
    view = _view(page)
    assert view["top"] <= view["cursorTop"] and view["cursorBottom"] <= view["bottom"]
    assert errors == []
    ctx.close()


def test_with_the_keyboard_up_a_full_screen_program_gets_the_shorter_height(
    browser,  # noqa: F811
    server,
):
    ctx, page, errors = _open(browser, server, **PHONE)
    page.tap("#nt-container")
    ready(page)
    rows = page.evaluate(f"{ENGINE}.size.rows")
    _keyboard(page, 340)
    run(page, "printf '\\033[?1049h'; sleep 2; printf '\\033[?1049l'")
    page.wait_for_function(f"{ENGINE}.size.rows < {rows}", timeout=8000)
    page.wait_for_function(f"{ENGINE}.size.rows === {rows}", timeout=8000)
    assert errors == []
    ctx.close()


def test_on_a_phone_jump_to_bottom_leaves_the_keyboard_as_it_was(
    browser,  # noqa: F811
    server,
):
    ctx, page, errors = _open(browser, server, **PHONE)
    ready(page)
    run(page, "seq 1 300")
    wait_text(page, "300", "screen")
    page.evaluate(f"{ENGINE}.scroller.scrollTop -= 400")
    page.wait_for_selector("#nt-jump:not([hidden])", timeout=5000)
    page.evaluate("document.activeElement.blur()")
    page.tap("#nt-jump")
    page.wait_for_function(f"{ENGINE}.renderer.stuck === true", timeout=5000)
    assert page.evaluate("document.activeElement.id") != "nt-input"

    # An open keyboard stays open.
    page.evaluate("document.getElementById('nt-input').focus()")
    page.evaluate(f"{ENGINE}.scroller.scrollTop -= 400")
    page.wait_for_selector("#nt-jump:not([hidden])", timeout=5000)
    page.tap("#nt-jump")
    page.wait_for_function(f"{ENGINE}.renderer.stuck === true", timeout=5000)
    assert page.evaluate("document.activeElement.id") == "nt-input"
    assert errors == []
    ctx.close()


def test_a_finger_held_on_the_terminal_selects_and_offers_to_copy(
    browser,  # noqa: F811
    server,
):
    """iOS will not select outside the focused field. The page selects, the
    layout waits for the selection, and the Copy pill writes the clipboard."""
    ctx, page, errors = _open(browser, server, **PHONE)
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    cdp = ctx.new_cdp_session(page)
    page.tap("#nt-container")
    ready(page)
    run(page, "seq 1 300")
    wait_text(page, "300", "screen")
    focused = "document.activeElement.id === 'nt-input'"
    height = "document.querySelector('.main').style.height"
    selected = "getSelection().toString()"
    pill = "document.getElementById('copy-toast').hidden"
    _keyboard(page, 340)
    assert page.evaluate(focused)

    def row(text: str) -> dict:
        return page.evaluate(
            """(text) => { const rows = [...document.querySelectorAll('.nt-hist .r')];
                const b = rows.filter(e => e.textContent.trim() === text).pop().getBoundingClientRect();
                return {x: b.left + 4, y: b.top + 6}; }""",
            text,
        )

    def touch(kind: str, point: dict | None = None) -> None:
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": kind, "touchPoints": [point] if point else []},
        )

    # A finger that moves is a scroll: the field keeps the focus.
    start = row("295")
    touch("touchStart", start)
    touch("touchMove", {"x": start["x"], "y": start["y"] + 40})
    page.wait_for_timeout(450)
    assert page.evaluate(focused) and page.evaluate(selected) == ""
    touch("touchEnd")

    # A finger that stays: the word, the layout does not move, the pill copies
    # and the keyboard's field gets the focus back.
    touch("touchStart", row("295"))
    page.wait_for_function(f"{selected}.trim() === '295'", timeout=3000)
    assert not page.evaluate(focused)
    _keyboard(page, None)
    assert page.evaluate(height) == "340px"
    touch("touchEnd")
    page.wait_for_function(f"!{pill}", timeout=3000)
    assert page.evaluate(height) == "340px"
    page.tap("#copy-toast")
    page.wait_for_function(
        "navigator.clipboard.readText().then(t => t.trim() === '295')", timeout=3000
    )
    page.wait_for_function(f"{height} === '' && {pill} && {focused}", timeout=3000)
    page.wait_for_function(
        f"{ENGINE}.renderer.viewH === {ENGINE}.scroller.clientHeight", timeout=3000
    )

    # A drag extends it; a selection given up lets the layout go.
    _keyboard(page, 340)
    touch("touchStart", row("295"))
    page.wait_for_function(f"{selected}.trim() === '295'", timeout=3000)
    target = row("297")
    touch("touchMove", target)
    page.wait_for_function(f"{selected}.includes('296')", timeout=3000)
    touch("touchEnd")
    page.wait_for_function(f"!{pill}", timeout=3000)
    _keyboard(page, None)
    page.evaluate("getSelection().removeAllRanges()")
    page.wait_for_function(f"{height} === '' && {pill}", timeout=3000)
    assert page.evaluate(f"{ENGINE}.scroller.style.overflow") == ""
    assert errors == []
    ctx.close()


def test_on_a_desktop_the_field_stays_out_of_the_way(page):
    box = page.locator("#nt-input").bounding_box()
    assert box["width"] <= 1
