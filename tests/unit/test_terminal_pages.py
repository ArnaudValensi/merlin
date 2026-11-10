"""The two terminal pages render the same chrome (shared partials) and the
switch between them (terminal-choice.js) runs first on both."""

import json
import re

import pytest
from fastapi.testclient import TestClient

import main as app_mod


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")
    monkeypatch.setattr(app_mod, "TMUX_AVAILABLE", True)


def _get(path: str) -> str:
    with TestClient(app_mod.app) as client:
        response = client.get(path)
    assert response.status_code == 200
    return response.text


SHARED_IDS = [
    "terminal-status",
    "status-dot",
    "status-text",
    "mic-btn",
    "mic-lang",
    "auto-enter-btn",
    "voice-btn",
    "toolbar-toggle",
    "notif-btn",
    "sessions-btn",
    "sessions-panel",
    "sessions-divider",
    "notif-popover",
    "voice-console",
    "folder-picker-modal",
    "paste-btn",
    "upload-btn",
    "cd-btn",
    "commits-btn",
    "new-window-btn",
    "upload-file-input",
]


@pytest.mark.parametrize("path", ["/terminal", "/terminal/native"])
def test_both_pages_carry_the_shared_chrome(path):
    html = _get(path)
    for element in SHARED_IDS:
        assert html.count(f'id="{element}"') == 1, element
    # The voice button reads its capability from the page context.
    assert re.search(r'id="voice-btn"[^>]*data-mode="[^"]+"', html, re.S)
    for script in (
        "board/board.js",
        "notifications/notifications.js",
        "folder-picker.js",
    ):
        assert html.count(script) == 1, script


@pytest.mark.parametrize(
    "path,kind", [("/terminal", "classic"), ("/terminal/native", "native")]
)
def test_the_switch_runs_before_anything_else(path, kind):
    html = _get(path)
    route = html.index(f"MerlinTerminalChoice.route('{kind}')")
    assert route < html.index('id="terminal-status"')
    assert route < html.index("board/board.js")


def test_no_page_has_a_terminal_menu_and_the_native_one_has_its_config():
    classic = _get("/terminal")
    native = _get("/terminal/native")
    # The choice lives in Merlin's settings; no keyboard suggestions setting.
    for html in (classic, native):
        assert "term-menu" not in html
        assert "suggestions" not in html.lower()
    config = re.search(
        r'<script type="application/json" id="nt-config">(.*?)</script>', native, re.S
    )
    assert config
    data = json.loads(config.group(1))
    assert set(data) == {"homeDir", "voiceAvailable"}
    assert isinstance(data["voiceAvailable"], bool)
    # No xterm.js renderer on the native page, its own scripts instead.
    assert "xterm-addon-fit" not in native
    assert "/static/terminal/native/page.js" in native


def test_the_choice_script_is_free_of_jinja_delimiters():
    """It is included as a template, so a stray delimiter is a 500 at runtime."""
    from terminal import routes as tr

    source = (tr.TERMINAL_TEMPLATES_DIR / "terminal-choice.js").read_text()
    for delimiter in ("{{", "{%", "{#"):
        assert delimiter not in source, delimiter
