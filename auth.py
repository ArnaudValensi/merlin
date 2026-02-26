# /// script
# dependencies = ["fastapi"]
# ///
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


def set_auth_cookie(response: Response, username: str, password: str, secure: bool = True) -> None:
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


def is_authenticated(request: Request) -> bool:
    """Check if a request has a valid session cookie."""
    password = _get_password()
    if not password:
        return True  # No auth configured — allow all

    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return False

    return verify_cookie(cookie, password) is not None


def require_auth(request: Request) -> None:
    """FastAPI dependency that redirects to /login if not authenticated.

    Usage: @app.get("/page", dependencies=[Depends(require_auth)])
    """
    password = _get_password()
    if not password:
        return  # No auth configured

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


def verify_ws_cookie(request: Request) -> bool:
    """Verify WebSocket auth via session cookie.

    Browsers send cookies on WebSocket upgrade requests to the same origin.
    """
    password = _get_password()
    if not password:
        return True

    # WebSocket: cookies are in the initial HTTP upgrade request
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return False

    return verify_cookie(cookie, password) is not None
