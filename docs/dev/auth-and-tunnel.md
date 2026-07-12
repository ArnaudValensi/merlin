# Authentication

Reference documentation for the cookie-based auth system.

## Overview

The dashboard is protected by cookie-based authentication (replacing HTTP Basic Auth): a login page and signed session cookies.

Merlin serves plain HTTP and does not manage remote exposure itself. HTTPS comes from whatever sits in front: a tunnel or reverse proxy the user brings, or the Merlin Cloud SSH tunnel in SaaS mode (`saas_tunnel.py`). The bundled cloudflared tunnel was removed; `TUNNEL_*` keys in old configs are ignored and scrubbed by `merlin setup`.

One route namespace is intentionally public: `/webhooks/*` (the webhooks front desk, mounted without `require_auth`) self-authenticates each request with a per-hook secret checked in constant time (a 256-bit token; flood protection is left to the edge). Everything under `/api` and the pages stays session-gated. See [`job-system.md`](job-system.md#webhook-trigger).

## Authentication

### Cookie Format

```
{username}:{expiry_timestamp}:{hmac_signature}
```

- **Cookie name**: `session`
- **Signature**: `HMAC-SHA256(DASHBOARD_PASS, "{username}:{expiry}")`
- **Expiry**: 30 days from login
- **Flags**: `httponly`, `samesite=lax`, and `secure` when the login request arrived over HTTPS (direct scheme or `x-forwarded-proto: https` from a fronting proxy)

No server-side session storage — cookies are self-contained and verified via HMAC.

### Auth Flow

```
Request arrives
  → Is route exempt? (/login, /static/*) → allow
  → Has valid session cookie? → allow
  → No/invalid cookie → redirect to /login?next={original_url}
```

### Login Page

- `GET /login` — renders dark-themed login form
- `POST /login` — validates password, sets cookie, redirects to `?next=` URL
- `GET /logout` — clears cookie, redirects to `/login`
- Password field supports browser autofill (`autocomplete="current-password"`)
- Error shown inline on wrong password

### No-Auth Mode

When `DASHBOARD_PASS` is empty, all routes are accessible without auth. Boot logs a warning that this is only fine local-only; anyone exposing the dashboard is expected to set a password.

### WebSocket Auth

Browsers send cookies on WebSocket upgrade requests. The terminal WebSocket (`/ws/terminal`) verifies the session cookie from the HTTP upgrade request:

```python
def verify_ws_cookie(request: Request) -> bool:
    cookie = request.cookies.get("session")
    return verify_cookie(cookie, password) is not None
```

Unauthorized WebSocket connections receive close code `4401`.

### Auth Module (`auth.py`)

Key functions:

| Function | Purpose |
|----------|---------|
| `sign_cookie(username, expiry, password)` | Create signed cookie value |
| `verify_cookie(cookie_value, password)` | Verify and return username or `None` |
| `set_auth_cookie(response, username, password)` | Set cookie on response |
| `clear_auth_cookie(response)` | Delete cookie |
| `require_auth(request)` | FastAPI dependency — redirects to `/login` |
| `verify_ws_cookie(request)` | Check WebSocket auth |
| `is_authenticated(request)` | Bool check for templates |

### Security Properties

- Cookies survive Merlin restarts (same password = same signing key)
- Changing password invalidates all existing cookies
- HMAC prevents cookie forgery
- `httponly` prevents JS access
- `secure` flag set when the request came over HTTPS
- Constant-time comparison (`hmac.compare_digest`)

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DASHBOARD_USER` | No | `admin` | Login username |
| `DASHBOARD_PASS` | No | empty (no auth) | Login password |

## Key Files

| File | Purpose |
|------|---------|
| `auth.py` | Cookie signing/verification, auth middleware |
| `main.py` | Login/logout routes, auth redirect handler |
| `templates/login.html` | Login page template |
