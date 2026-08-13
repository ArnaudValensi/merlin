"""Guards for terminal toolbar actions that bypass terminal key emulation."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERMINAL_HTML = ROOT / "terminal" / "templates" / "terminal.html"
BOARD_JS = ROOT / "board" / "static" / "board.js"


def test_plus_button_uses_sessions_panel_window_creation_path():
    """The toolbar plus must not depend on a stale tmux F2 binding."""
    terminal = TERMINAL_HTML.read_text()
    button = re.search(r'<button[^>]+id="new-window-btn"[^>]*>', terminal)

    assert button is not None
    assert "data-key" not in button.group()
    assert "window.SessionsBoard.openNewWindow(currentSession)" in terminal


def test_sessions_panel_and_toolbar_share_open_new_window_action():
    board = BOARD_JS.read_text()

    assert "function openNewWindow(sessionName)" in board
    assert "openNewWindow(sessionName);" in board
    assert "openNewWindow: openNewWindow" in board
