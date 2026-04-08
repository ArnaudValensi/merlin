"""Cookie-based authentication for the dashboard.

HMAC-signed session cookies — no server-side session storage.

Cookie format: {username}:{expiry_timestamp}:{signature}
  - signature = HMAC-SHA256(password, username + ":" + expiry)
  - Survives restarts (same password = same signing key)
  - Invalidated automatically on password change

Usage:
    from auth import require_auth, verify_cookie, configure

    # Set password at startup:
    configure(password="secret")

    # As a FastAPI dependency on individual routes:
    @app.get("/page", dependencies=[Depends(require_auth)])

    # Or on a router:
    app.include_router(router, dependencies=[Depends(require_auth)])
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets as _secrets
import time

from fastapi import Request, Response

# Cookie settings
COOKIE_NAME = "session"
COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days in seconds

# Module-level password — set by the entry point via configure()
_dashboard_password: str = ""


def configure(password: str) -> None:
    """Set the dashboard password for auth verification."""
    global _dashboard_password
    _dashboard_password = password


def _get_password() -> str:
    """Get the current dashboard password."""
    return _dashboard_password


def _check_portal_auth(request: Request) -> bool:
    """Check X-Portal-Auth header against MERLIN_SAAS_TOKEN.

    Used by the portal proxy to bypass dashboard password auth when the user
    is authenticated with Clerk. Returns True if the header matches the token.
    """
    header = request.headers.get("x-portal-auth", "")
    saas_token = os.environ.get("MERLIN_SAAS_TOKEN", "")
    if not header or not saas_token:
        return False
    return _secrets.compare_digest(header.encode(), saas_token.encode())


def sign_cookie(username: str, expiry: int, password: str) -> str:
    """Create a signed cookie value: username:expiry:signature."""
    payload = f"{username}:{expiry}"
    sig = hmac.new(
        password.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{sig}"


def verify_cookie(cookie_value: str, password: str) -> str | None:
    """Verify a signed cookie. Returns the username if valid, None otherwise."""
    if not cookie_value or not password:
        return None

    parts = cookie_value.split(":", 2)
    if len(parts) != 3:
        return None

    username, expiry_str, signature = parts

    # Check expiry
    try:
        expiry = int(expiry_str)
    except ValueError:
        return None

    if time.time() > expiry:
        return None

    # Verify HMAC signature
    expected_payload = f"{username}:{expiry_str}"
    expected_sig = hmac.new(
        password.encode("utf-8"),
        expected_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_sig):
        return None

    return username


def set_auth_cookie(
    response: Response, username: str, password: str, secure: bool = True
) -> None:
    """Set the signed session cookie on a response."""
    expiry = int(time.time()) + COOKIE_MAX_AGE
    value = sign_cookie(username, expiry, password)
    response.set_cookie(
        key=COOKIE_NAME,
        value=value,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def clear_auth_cookie(response: Response) -> None:
    """Clear the session cookie."""
    response.delete_cookie(key=COOKIE_NAME)


def _is_saas_mode() -> bool:
    """Check if running in SaaS mode (portal-managed)."""
    return bool(os.environ.get("MERLIN_SAAS_TOKEN", ""))


def is_authenticated(request: Request) -> bool:
    """Check if a request has a valid session cookie or portal auth header."""
    if _check_portal_auth(request):
        return True

    password = _get_password()
    if not password:
        if _is_saas_mode():
            return False  # SaaS mode: only portal auth allowed
        return True  # No auth configured — allow all (local mode)

    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return False

    return verify_cookie(cookie, password) is not None


def require_auth(request: Request) -> None:
    """FastAPI dependency that redirects to /login if not authenticated.

    In SaaS mode, redirects to merlincloud.dev instead of the login form.
    Usage: @app.get("/page", dependencies=[Depends(require_auth)])
    """
    if _check_portal_auth(request):
        return

    password = _get_password()
    if not password:
        if _is_saas_mode():
            raise _SaaSAuthRedirect()
        return  # No auth configured (local mode)

    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and verify_cookie(cookie, password) is not None:
        return

    # Redirect to login with the original URL as ?next=
    next_url = request.url.path
    if request.url.query:
        next_url += f"?{request.url.query}"
    raise _AuthRedirect(next_url)


class _AuthRedirect(Exception):
    """Raised by require_auth to trigger a redirect to /login."""

    def __init__(self, next_url: str):
        self.next_url = next_url


class _SaaSAuthRedirect(Exception):
    """Raised by require_auth in SaaS mode to redirect to the portal."""

    pass


def verify_ws_cookie(request: Request) -> bool:
    """Verify WebSocket auth via session cookie or portal auth header.

    Browsers send cookies on WebSocket upgrade requests to the same origin.
    """
    if _check_portal_auth(request):
        return True

    password = _get_password()
    if not password:
        if _is_saas_mode():
            return False  # SaaS mode: only portal auth allowed
        return True  # Local mode: no auth configured

    # WebSocket: cookies are in the initial HTTP upgrade request
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return False

    return verify_cookie(cookie, password) is not None
