"""Guards for terminal toolbar actions that bypass terminal key emulation."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERMINAL_HTML = ROOT / "terminal" / "templates" / "terminal.html"
NATIVE_HTML = ROOT / "terminal" / "templates" / "terminal-native.html"
NATIVE_PAGE_JS = ROOT / "terminal" / "static" / "native" / "page.js"
BOARD_JS = ROOT / "board" / "static" / "board.js"


def test_plus_button_uses_sessions_panel_window_creation_path():
    """The toolbar plus must not depend on a stale tmux F2 binding."""
    terminal = TERMINAL_HTML.read_text()
    button = re.search(r'<button[^>]+id="new-window-btn"[^>]*>', terminal)

    assert button is not None
    assert "data-key" not in button.group()
    assert "window.SessionsBoard.openNewWindow(currentSession)" in terminal


def test_the_native_plus_button_uses_the_same_creation_path():
    """The native toolbar's plus is an action, not a key: page.js hands it to
    the Sessions panel's create-and-switch path (F2 only without the panel)."""
    button = re.search(
        r'<button[^>]+id="new-window-btn"[^>]*>', NATIVE_HTML.read_text()
    )

    assert button is not None
    assert "data-key" not in button.group()
    assert 'data-action="new-window"' in button.group()
    assert "board.openNewWindow(currentSession)" in NATIVE_PAGE_JS.read_text()


def test_sessions_panel_and_toolbar_share_open_new_window_action():
    board = BOARD_JS.read_text()

    assert "function openNewWindow(sessionName)" in board
    assert "openNewWindow(sessionName);" in board
    assert "openNewWindow: openNewWindow" in board
