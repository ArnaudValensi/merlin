"""The terminal page: /terminal renders the native page with its chrome, and
/terminal/native (its old address) redirects there with the query."""

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


def _get(path: str) -> str:
    with TestClient(app_mod.app) as client:
        response = client.get(path)
    assert response.status_code == 200
    return response.text


CHROME_IDS = [
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
    "nt-container",
    "nt-input",
    "nt-notice",
]


def test_the_terminal_page_carries_its_chrome():
    html = _get("/terminal")
    for element in CHROME_IDS:
        assert html.count(f'id="{element}"') == 1, element
    # The voice button reads its capability from the page context.
    assert re.search(r'id="voice-btn"[^>]*data-mode="[^"]+"', html, re.S)
    for script in (
        "board/board.js",
        "notifications/notifications.js",
        "folder-picker.js",
        "/static/terminal/native/page.js",
    ):
        assert html.count(script) == 1, script
    # The decoder's parser and the Unicode 11 widths, nothing else of xterm.js.
    assert "/static/vendor/xterm.min.js" in html
    assert "/static/vendor/xterm-addon-unicode11.min.js" in html
    assert "xterm-addon-fit" not in html
    assert "xterm-addon-web-links" not in html
    assert "xterm.min.css" not in html


def test_the_page_has_its_config_and_no_switch():
    html = _get("/terminal")
    # The choice between two terminals is gone, with its menu and parameter.
    for gone in (
        "MerlinTerminalChoice",
        "terminal-choice",
        "term-menu",
        "term-fallback",
    ):
        assert gone not in html, gone
    assert "suggestions" not in html.lower()
    config = re.search(
        r'<script type="application/json" id="nt-config">(.*?)</script>', html, re.S
    )
    assert config
    data = json.loads(config.group(1))
    assert set(data) == {"homeDir", "voiceAvailable"}
    assert isinstance(data["voiceAvailable"], bool)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/terminal/native", "/terminal"),
        ("/terminal/native?target=dev:@3", "/terminal?target=dev:@3"),
        (
            "/terminal/native?terminal=classic&target=dev:@3",
            "/terminal?terminal=classic&target=dev:@3",
        ),
    ],
)
def test_the_old_native_address_redirects_with_its_query(path, expected):
    with TestClient(app_mod.app) as client:
        response = client.get(path, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == expected
    with TestClient(app_mod.app) as client:
        followed = client.get(path)
    assert followed.status_code == 200
    assert 'id="nt-container"' in followed.text


def test_the_terminal_parameter_is_ignored():
    html = _get("/terminal?terminal=classic")
    assert 'id="nt-container"' in html
    assert "xterm-screen" not in html


def test_the_settings_page_has_no_terminal_section_and_drops_the_old_key():
    html = _get("/settings")
    assert "terminal-choice" not in html
    assert "toast-terminal" not in html
    assert ">Legacy<" not in html
    js = _get("/static/settings.js")
    assert "localStorage.removeItem('merlin-terminal')" in js
    assert "setTerminal" not in js and "loadTerminal" not in js
