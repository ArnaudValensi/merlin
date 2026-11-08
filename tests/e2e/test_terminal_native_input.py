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
    page.evaluate(
        """() => {
            const l = [...document.querySelectorAll('.nt-content .l')].filter(x => x.textContent.startsWith('COPYTHIS')).pop();
            const r = document.createRange();
            r.selectNodeContents(l);
            getSelection().removeAllRanges();
            getSelection().addRange(r);
        }"""
    )
    page.keyboard.press("Control+Shift+C")
    page.wait_for_function("navigator.clipboard.readText().then(t => t === 'COPYTHIS')")


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
