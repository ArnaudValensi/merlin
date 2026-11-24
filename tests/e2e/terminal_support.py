"""Opening the web terminal in the e2e suite, and reading what it shows.

The terminal is the native page (``terminal-native.html``, ``/ws/terminal2``,
``terminal/static/native/page.js``): tmux's grid as real DOM text. Every e2e
file that needs a terminal opens it through ``open_terminal`` and reads its
screen through ``screen_text``, so a change of the page's internals is one
change here.
"""

import json
import time

ENGINE = "window.MerlinNativeTerminalPage.engine"
SCREEN = f"{ENGINE}.renderer.screenText()"
# The terminal itself (click it to focus the input), and the input field a
# key goes through.
TERMINAL = "#nt-container"
INPUT = "#nt-input"
DESKTOP = {"viewport": {"width": 1100, "height": 720}}


def open_terminal(page, server: str, query: str = "", path: str = "/terminal") -> None:
    """``/terminal`` (the native page) with ``query`` (``?target=...``), attached:
    the bridge sent its first pane and the page knows its window and its size."""
    page.goto(f"{server}{path}{query}")
    wait_native(page)


def wait_native(page) -> None:
    page.wait_for_function(
        f"window.MerlinNativeTerminalPage && {ENGINE}.pane"
        " && window.MerlinTerminal.currentWindow()"
        f" && {ENGINE}.size.cols === {ENGINE}.renderer.cols",
        timeout=30000,
    )


def screen_text(page) -> str:
    return page.evaluate(SCREEN)


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
    """A clean prompt at the bottom: nothing typed, nothing above it."""
    page.click(TERMINAL)
    page.keyboard.press("Control+u")
    page.keyboard.type("clear")
    page.keyboard.press("Enter")
    ready(page)


def count(page, marker: str) -> int:
    return screen_text(page).count(marker)


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
