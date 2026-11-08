"""Template contract for per-tab terminal session restoration."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "terminal" / "templates"
TERMINAL = (TEMPLATES / "terminal.html").read_text()
# Both terminal pages get the helper through their shared chrome.
CHROME = (TEMPLATES / "terminal-chrome.html").read_text()
NATIVE = (ROOT / "terminal" / "static" / "native" / "page.js").read_text()


def test_terminal_includes_the_tested_session_identity_helper():
    assert '{% include "session-identity.js" %}' in CHROME
    for page in ("terminal.html", "terminal-native.html"):
        assert '{% include "terminal-chrome.html" %}' in (TEMPLATES / page).read_text()


def test_the_native_page_uses_the_same_identity_contract():
    assert "MerlinSessionIdentity.read()" in NATIVE
    assert "MerlinSessionIdentity.websocketUrl(location, attempted)" in NATIVE
    assert "MerlinSessionIdentity.confirm(msg)" in NATIVE
    assert (
        "MerlinSessionIdentity.clearAfterClose(attempted, confirmed, e.code)" in NATIVE
    )


def test_websocket_uses_stored_identity_and_confirms_server_frames():
    assert "MerlinSessionIdentity.read()" in TERMINAL
    assert "MerlinSessionIdentity.websocketUrl(location, attemptedIdentity)" in TERMINAL
    assert "MerlinSessionIdentity.confirm(ctl)" in TERMINAL


def test_confirmed_tmux_context_updates_the_page_title():
    assert "MerlinPageTitle.tmuxContext(ctl.name, ctl.window)" in TERMINAL
    assert "MerlinPageTitle.set(" in TERMINAL


def test_unconfirmed_connection_conditionally_clears_its_attempt():
    assert "MerlinSessionIdentity.clearAfterClose(" in TERMINAL
    assert "attemptedIdentity, sessionConfirmed, e.code" in TERMINAL
