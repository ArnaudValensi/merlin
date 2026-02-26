# Journal: Cloudflare Tunnel — Phase 1 & 2 Implementation

**Date:** 2026-02-15

## Work Done

### Phase 1: Infrastructure (complete)

**1.1 — cloudflared installed** via curl from GitHub releases. Version 2026.2.0, linux/amd64. Placed at `/usr/local/bin/cloudflared`. No Dockerfile yet (will add when Docker setup epic lands).

**1.2 — Fail-fast validation** added to `_validate_config()` in merlin.py. Checks `shutil.which("cloudflared")` only when `TUNNEL_ENABLED` is true (defaults to true). Provides install instructions and suggests `TUNNEL_ENABLED=false` to disable.

**1.3 — tunnel.py module** created with:
- `start_tunnel()` — async entry point, dispatches to Quick or Named mode
- Quick Tunnel: spawns `cloudflared tunnel --url http://localhost:{port}`, parses URL from stderr via regex
- Named Tunnel: spawns `cloudflared tunnel run --token {token}`, uses configured hostname
- `_parse_url_from_stderr()` — reads lines, matches `https://*.trycloudflare.com`, drains pipe in background
- `stop_tunnel()` — terminate with 5s timeout, fallback to kill
- Exponential backoff on crash (configurable max_restarts, default 5)
- Module-level state: `_public_url`, `_status` (stopped/starting/running/error)

**1.4 — Integrated into merlin.py** via `_start_tunnel_task()` launched in `on_ready()`. Added `[tunnel]` log prefix.

### Phase 2: Authentication (complete)

**2.1 — auth.py module** created with HMAC-SHA256 signed cookies:
- `sign_cookie(username, expiry, password)` → `username:expiry:hmac_hex`
- `verify_cookie(value, password)` → username or None (checks expiry + HMAC)
- `set_auth_cookie()` / `clear_auth_cookie()` — with HttpOnly, Secure, SameSite=Lax
- `require_auth()` — FastAPI dependency, raises `_AuthRedirect` → exception handler redirects to `/login?next=...`
- `verify_ws_cookie()` — for WebSocket upgrade requests
- `is_authenticated()` — for general request checking
- No server-side session storage; cookie is self-contained

**2.2 — Login page** (`templates/login.html`):
- Matches dashboard dark theme (same CSS variables)
- Password field with `autocomplete="current-password"` for browser autofill
- `?next=` parameter for redirect after login
- Inline error message on wrong password (server-rendered, no JS needed)
- Logout link added to sidebar footer in `base.html`

**2.3 — Auth migration** complete:
- Removed `HTTPBasic`, `HTTPBasicCredentials`, `verify_credentials`, `_security` from dashboard.py
- Removed `verify_page_auth`, `_verify_ws_auth`, `_get_credentials`, `_security` from terminal/routes.py
- Removed `ws_token` template variable, `WS_TOKEN` JS constant, `?token=` WebSocket param, `Authorization` header on fetch
- All routes now use `Depends(require_auth)` (pages redirect, API returns redirect)
- WebSocket uses `verify_ws_cookie()` — browser sends cookie automatically on upgrade

**2.4 — Auto-generate password** when `TUNNEL_ENABLED=true` and `DASHBOARD_PASS` is empty:
- Uses `secrets.token_urlsafe(12)`
- Sets both `os.environ["DASHBOARD_PASS"]` and module globals
- Logged at startup: `Login: admin / <generated>`

### Phase 3: Resilience (partially complete)

**3.1 — Crash detection** built into tunnel.py's start functions (exponential backoff, max restarts).

**3.2 — Dashboard health API** now returns `tunnel_url` and `tunnel_status`. Overview page shows a "Tunnel" card when active.

## Files Modified

| File | Change |
|------|--------|
| `merlin.py` | Added TUNNEL_* config, cloudflared validation, tunnel logger, `_start_tunnel_task()`, auto-password generation |
| `dashboard.py` | Replaced Basic Auth with cookie auth, added `/login` + `/logout`, tunnel status in health API |
| `terminal/routes.py` | Replaced Basic Auth with cookie auth, removed ws_token |
| `terminal/templates/terminal.html` | Removed WS_TOKEN, token param, Authorization header |
| `templates/base.html` | Added logout link in sidebar footer |
| `templates/login.html` | New login page (dark theme) |
| `templates/overview.html` | Added tunnel status card |
| `static/dashboard.css` | Added sidebar-footer and logout-link styles |
| `.env.example` | Added TUNNEL_* variables |

## Files Created

| File | Purpose |
|------|---------|
| `tunnel.py` | Cloudflare Tunnel manager (start/stop/monitor) |
| `auth.py` | Cookie-based authentication (sign/verify/middleware) |
| `tests/test_auth.py` | 27 tests for auth module |
| `tests/test_tunnel.py` | 9 tests for tunnel module |

## Test Results

376 tests pass (was 316 before — added 60 new tests across auth, tunnel, and updated terminal tests).

## Remaining

- 4.1: End-to-end test (manual — start tunnel, verify HTTPS, login, voice, terminal, reconnect)
- 2.4 partial: Send auto-generated password via Discord
- 3.2 partial: Copy-to-clipboard button for tunnel URL
