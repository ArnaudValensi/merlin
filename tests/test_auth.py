# /// script
# dependencies = ["pytest"]
# ///
"""Tests for auth.py — cookie-based authentication."""

import time
from unittest import mock

import pytest

import auth


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_dashboard(monkeypatch):
    """Mock dashboard credentials for all tests."""
    monkeypatch.setattr(auth, "_get_password", lambda: "secret123")


# ---------------------------------------------------------------------------
# Cookie signing / verification
# ---------------------------------------------------------------------------

class TestSignCookie:
    """sign_cookie produces a valid cookie string."""

    def test_format(self):
        result = auth.sign_cookie("admin", 9999999999, "secret123")
        parts = result.split(":", 2)
        assert len(parts) == 3
        assert parts[0] == "admin"
        assert parts[1] == "9999999999"
        assert len(parts[2]) == 64  # sha256 hex digest

    def test_deterministic(self):
        a = auth.sign_cookie("admin", 12345, "pass")
        b = auth.sign_cookie("admin", 12345, "pass")
        assert a == b

    def test_different_password_different_sig(self):
        a = auth.sign_cookie("admin", 12345, "pass1")
        b = auth.sign_cookie("admin", 12345, "pass2")
        assert a != b

    def test_different_username_different_sig(self):
        a = auth.sign_cookie("admin", 12345, "pass")
        b = auth.sign_cookie("hacker", 12345, "pass")
        assert a != b


class TestVerifyCookie:
    """verify_cookie validates HMAC and expiry."""

    def test_valid_cookie(self):
        cookie = auth.sign_cookie("admin", int(time.time()) + 3600, "secret123")
        assert auth.verify_cookie(cookie, "secret123") == "admin"

    def test_expired_cookie(self):
        cookie = auth.sign_cookie("admin", int(time.time()) - 1, "secret123")
        assert auth.verify_cookie(cookie, "secret123") is None

    def test_wrong_password(self):
        cookie = auth.sign_cookie("admin", int(time.time()) + 3600, "secret123")
        assert auth.verify_cookie(cookie, "wrongpass") is None

    def test_tampered_username(self):
        cookie = auth.sign_cookie("admin", int(time.time()) + 3600, "secret123")
        tampered = "hacker" + cookie[5:]
        assert auth.verify_cookie(tampered, "secret123") is None

    def test_empty_cookie(self):
        assert auth.verify_cookie("", "secret123") is None

    def test_none_cookie(self):
        assert auth.verify_cookie(None, "secret123") is None

    def test_no_password(self):
        cookie = auth.sign_cookie("admin", int(time.time()) + 3600, "secret123")
        assert auth.verify_cookie(cookie, "") is None

    def test_malformed_cookie_no_colons(self):
        assert auth.verify_cookie("nocolons", "secret123") is None

    def test_malformed_cookie_one_colon(self):
        assert auth.verify_cookie("only:one", "secret123") is None

    def test_non_numeric_expiry(self):
        assert auth.verify_cookie("admin:notanumber:somesig", "secret123") is None

    def test_survives_restart(self):
        """Same password = same signing key = cookie still valid."""
        cookie = auth.sign_cookie("admin", int(time.time()) + 3600, "mypass")
        # Simulate restart: verify with the same password
        assert auth.verify_cookie(cookie, "mypass") == "admin"

    def test_invalidated_on_password_change(self):
        """New password = old cookies rejected."""
        cookie = auth.sign_cookie("admin", int(time.time()) + 3600, "oldpass")
        assert auth.verify_cookie(cookie, "newpass") is None


# ---------------------------------------------------------------------------
# Cookie set/clear
# ---------------------------------------------------------------------------

class TestSetAuthCookie:
    """set_auth_cookie sets the correct cookie on a response."""

    def test_sets_cookie(self):
        response = mock.Mock()
        auth.set_auth_cookie(response, "admin", "secret123", secure=True)
        response.set_cookie.assert_called_once()
        kwargs = response.set_cookie.call_args.kwargs
        assert kwargs["key"] == "session"
        assert "admin:" in kwargs["value"]
        assert kwargs["httponly"] is True
        assert kwargs["secure"] is True
        assert kwargs["samesite"] == "lax"

    def test_secure_false_on_localhost(self):
        response = mock.Mock()
        auth.set_auth_cookie(response, "admin", "secret123", secure=False)
        kwargs = response.set_cookie.call_args.kwargs
        assert kwargs["secure"] is False


class TestClearAuthCookie:
    """clear_auth_cookie removes the session cookie."""

    def test_clears_cookie(self):
        response = mock.Mock()
        auth.clear_auth_cookie(response)
        response.delete_cookie.assert_called_once_with(key="session")


# ---------------------------------------------------------------------------
# Request auth checking
# ---------------------------------------------------------------------------

class TestIsAuthenticated:
    """is_authenticated checks request cookies."""

    def test_no_password_always_authenticated(self, monkeypatch):
        monkeypatch.setattr(auth, "_get_password", lambda: "")
        request = mock.Mock()
        request.cookies = {}
        assert auth.is_authenticated(request) is True

    def test_valid_cookie(self):
        cookie = auth.sign_cookie("admin", int(time.time()) + 3600, "secret123")
        request = mock.Mock()
        request.cookies = {"session": cookie}
        assert auth.is_authenticated(request) is True

    def test_no_cookie(self):
        request = mock.Mock()
        request.cookies = {}
        assert auth.is_authenticated(request) is False

    def test_invalid_cookie(self):
        request = mock.Mock()
        request.cookies = {"session": "garbage:value:here"}
        assert auth.is_authenticated(request) is False


# ---------------------------------------------------------------------------
# WebSocket cookie verification
# ---------------------------------------------------------------------------

class TestVerifyWsCookie:
    """verify_ws_cookie checks cookies on WebSocket requests."""

    def test_valid_cookie(self):
        cookie = auth.sign_cookie("admin", int(time.time()) + 3600, "secret123")
        request = mock.Mock()
        request.cookies = {"session": cookie}
        assert auth.verify_ws_cookie(request) is True

    def test_no_cookie(self):
        request = mock.Mock()
        request.cookies = {}
        assert auth.verify_ws_cookie(request) is False

    def test_no_password_allows_all(self, monkeypatch):
        monkeypatch.setattr(auth, "_get_password", lambda: "")
        request = mock.Mock()
        request.cookies = {}
        assert auth.verify_ws_cookie(request) is True

    def test_expired_cookie_rejected(self):
        cookie = auth.sign_cookie("admin", int(time.time()) - 1, "secret123")
        request = mock.Mock()
        request.cookies = {"session": cookie}
        assert auth.verify_ws_cookie(request) is False


# ---------------------------------------------------------------------------
# require_auth dependency
# ---------------------------------------------------------------------------

class TestRequireAuth:
    """require_auth raises _AuthRedirect or passes through."""

    def _make_request(self, path="/overview", query="", cookies=None):
        request = mock.Mock()
        request.cookies = cookies or {}
        request.url.path = path
        request.url.query = query
        return request

    def test_no_password_allows_all(self, monkeypatch):
        """When DASHBOARD_PASS is empty, everything passes."""
        monkeypatch.setattr(auth, "_get_password", lambda: "")
        request = self._make_request(cookies={})
        # Should not raise
        auth.require_auth(request)

    def test_valid_cookie_passes(self):
        cookie = auth.sign_cookie("admin", int(time.time()) + 3600, "secret123")
        request = self._make_request(cookies={"session": cookie})
        # Should not raise
        auth.require_auth(request)

    def test_no_cookie_redirects(self):
        request = self._make_request(path="/overview")
        with pytest.raises(auth._AuthRedirect) as exc_info:
            auth.require_auth(request)
        assert exc_info.value.next_url == "/overview"

    def test_expired_cookie_redirects(self):
        cookie = auth.sign_cookie("admin", int(time.time()) - 1, "secret123")
        request = self._make_request(path="/logs", cookies={"session": cookie})
        with pytest.raises(auth._AuthRedirect) as exc_info:
            auth.require_auth(request)
        assert exc_info.value.next_url == "/logs"

    def test_invalid_cookie_redirects(self):
        request = self._make_request(
            path="/terminal", cookies={"session": "tampered:garbage:data"}
        )
        with pytest.raises(auth._AuthRedirect):
            auth.require_auth(request)

    def test_preserves_query_string(self):
        request = self._make_request(path="/logs", query="type=error&since=2026-01-01")
        with pytest.raises(auth._AuthRedirect) as exc_info:
            auth.require_auth(request)
        assert exc_info.value.next_url == "/logs?type=error&since=2026-01-01"

    def test_no_query_string(self):
        request = self._make_request(path="/performance", query="")
        with pytest.raises(auth._AuthRedirect) as exc_info:
            auth.require_auth(request)
        assert exc_info.value.next_url == "/performance"


# ---------------------------------------------------------------------------
# Open redirect protection
# ---------------------------------------------------------------------------

class TestSafeNextUrl:
    """_safe_next_url prevents open redirects."""

    def test_relative_path_allowed(self):
        from main import _safe_next_url
        assert _safe_next_url("/overview") == "/overview"

    def test_relative_path_with_query(self):
        from main import _safe_next_url
        assert _safe_next_url("/logs?type=error") == "/logs?type=error"

    def test_absolute_url_blocked(self):
        from main import _safe_next_url
        assert _safe_next_url("https://evil.com") == "/files"

    def test_protocol_relative_blocked(self):
        from main import _safe_next_url
        assert _safe_next_url("//evil.com") == "/files"

    def test_empty_string_blocked(self):
        from main import _safe_next_url
        assert _safe_next_url("") == "/files"

    def test_javascript_uri_blocked(self):
        from main import _safe_next_url
        assert _safe_next_url("javascript:alert(1)") == "/files"

    def test_data_uri_blocked(self):
        from main import _safe_next_url
        assert _safe_next_url("data:text/html,<h1>hi</h1>") == "/files"
