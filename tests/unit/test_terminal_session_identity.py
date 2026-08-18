"""Template contract for per-tab terminal session restoration."""

from pathlib import Path

TERMINAL = (
    Path(__file__).resolve().parents[2] / "terminal" / "templates" / "terminal.html"
).read_text()


def test_terminal_includes_the_tested_session_identity_helper():
    assert '{% include "session-identity.js" %}' in TERMINAL


def test_websocket_uses_stored_identity_and_confirms_server_frames():
    assert "MerlinSessionIdentity.read()" in TERMINAL
    assert "MerlinSessionIdentity.websocketUrl(location, attemptedIdentity)" in TERMINAL
    assert "MerlinSessionIdentity.confirm(ctl)" in TERMINAL


def test_unconfirmed_connection_conditionally_clears_its_attempt():
    assert "MerlinSessionIdentity.clearAfterClose(" in TERMINAL
    assert "attemptedIdentity, sessionConfirmed, e.code" in TERMINAL
