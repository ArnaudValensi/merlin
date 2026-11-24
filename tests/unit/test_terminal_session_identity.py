"""Template contract for per-tab terminal session restoration."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "terminal" / "templates"
# The terminal page gets the helper through its chrome.
CHROME = (TEMPLATES / "terminal-chrome.html").read_text()
NATIVE = (ROOT / "terminal" / "static" / "native" / "page.js").read_text()


def test_terminal_includes_the_tested_session_identity_helper():
    assert '{% include "session-identity.js" %}' in CHROME
    assert (
        '{% include "terminal-chrome.html" %}'
        in (TEMPLATES / "terminal-native.html").read_text()
    )


def test_the_page_uses_the_identity_contract():
    assert "MerlinSessionIdentity.read()" in NATIVE
    assert "MerlinSessionIdentity.websocketUrl(location, attempted)" in NATIVE
    assert "MerlinSessionIdentity.confirm(msg)" in NATIVE
    assert (
        "MerlinSessionIdentity.clearAfterClose(attempted, confirmed, e.code)" in NATIVE
    )


def test_confirmed_tmux_context_updates_the_page_title():
    assert "MerlinPageTitle.tmuxContext(msg.name, msg.window)" in NATIVE
    assert "MerlinPageTitle.set(" in NATIVE
