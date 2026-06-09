# Authentication & Cloudflare Tunnel

Reference documentation for the cookie-based auth system and Cloudflare Tunnel integration.

## Overview

The dashboard is protected by cookie-based authentication (replacing HTTP Basic Auth). When exposed publicly via Cloudflare Tunnel, the system provides HTTPS with a login page and signed session cookies.

## Authentication

### Cookie Format

```
{username}:{expiry_timestamp}:{hmac_signature}
```

- **Cookie name**: `session`
- **Signature**: `HMAC-SHA256(DASHBOARD_PASS, "{username}:{expiry}")`
- **Expiry**: 30 days from login
- **Flags**: `httponly`, `secure` (when tunnel active), `samesite=lax`

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

When `DASHBOARD_PASS` is empty and tunnel is disabled, all routes are accessible without auth (local-only mode).

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
- `secure` flag set when tunnel active (HTTPS)
- Constant-time comparison (`hmac.compare_digest`)

## Cloudflare Tunnel

### Two Modes

**Quick Tunnel** (default, no config needed):
```bash
cloudflared tunnel --url http://localhost:3123
```
- Generates random `https://<random>.trycloudflare.com` URL
- URL changes on each restart
- URL parsed from cloudflared's stderr output

**Named Tunnel** (stable domain):
```bash
cloudflared tunnel run --token $TUNNEL_TOKEN
```
- Requires `TUNNEL_TOKEN` from Cloudflare dashboard
- Optional `TUNNEL_HOSTNAME` for custom domain
- Stable URL across restarts

### Tunnel Module (`tunnel.py`)

Module-level state:
- `_public_url` — current tunnel URL (or `None`)
- `_status` — `stopped | starting | running | error`
- `_process` — `asyncio.subprocess.Process`

Key functions:

| Function | Purpose |
|----------|---------|
| `start_tunnel(port, token, hostname, ...)` | Start and monitor tunnel |
| `stop_tunnel()` | Graceful shutdown (terminate, then kill) |
| `get_public_url()` | Get current URL |
| `get_status()` | Get current status |

### Crash Recovery

- Exponential backoff: `delay * 2^(restarts-1)` starting at 5s
- Max 5 consecutive restarts before giving up
- On give-up: logs error, continues without tunnel
- Clean shutdown: `terminate()` with 5s timeout, then `kill()`

### URL Detection (Quick Tunnel)

Cloudflared prints the URL to stderr:
```
+-----------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at ...      |
|  https://random-thing.trycloudflare.com                   |
+-----------------------------------------------------------+
```

Parsed via regex: `https://[a-zA-Z0-9._-]+\.trycloudflare\.com`

After URL found, stderr is drained in background to prevent pipe blocking.

### Auto-Generated Password

When tunnel is active and `DASHBOARD_PASS` is empty:
1. Generate random password: `secrets.token_urlsafe(12)`
2. Set as active `DASHBOARD_PASS`
3. Log credentials: `[bot] Login: admin / <password>`
4. Optionally send to Discord

### Dashboard Integration

Tunnel status shown on the overview page:
- `/api/health` returns `tunnel_url` and `tunnel_status`
- Overview card shows URL with copy-to-clipboard button

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DASHBOARD_USER` | No | `admin` | Login username |
| `DASHBOARD_PASS` | No | auto-gen | Login password |
| `TUNNEL_ENABLED` | No | `true` | Enable/disable tunnel |
| `TUNNEL_TOKEN` | No | — | Named Tunnel auth token |
| `TUNNEL_HOSTNAME` | No | — | Custom tunnel hostname |

## Key Files

| File | Purpose |
|------|---------|
| `auth.py` | Cookie signing/verification, auth middleware |
| `tunnel.py` | Cloudflare Tunnel lifecycle management |
| `main.py` | Login/logout routes, auth redirect handler, tunnel startup, password auto-gen |
| `templates/login.html` | Login page template |
