# Tasks: Cloudflare Tunnel

## Phase 1: Infrastructure

### 1.1 Install cloudflared in Docker
- [x] Add `cloudflared` binary to Dockerfile (official release, linux/amd64+arm64)
- [x] Verify binary works: `cloudflared --version`
- [x] Keep image size minimal (download + chmod, no package manager)
- **Assignee:** Merlin
- **Depends on:** —
- **Validation:** `cloudflared --version` returns `2026.2.0`
- **Note:** Installed directly via curl (no Dockerfile yet — will add when Docker setup epic lands)

### 1.2 Fail-fast validation
- [x] Add `cloudflared` check to `_validate_config()` in `merlin.py`
- [x] If `cloudflared` not found in PATH: exit with clear message and install instructions
- [x] If `TUNNEL_ENABLED=false`: skip the check
- **Assignee:** Merlin
- **Depends on:** 1.1
- **Validation:** Conditional check in `_validate_config()` — skipped when `TUNNEL_ENABLED=false`

### 1.3 Tunnel manager module
- [x] Create `tunnel.py` module with `start_tunnel()` async function
- [x] Read config: `TUNNEL_ENABLED`, `TUNNEL_TOKEN`, `TUNNEL_HOSTNAME` from env
- [x] Quick Tunnel mode: spawn `cloudflared tunnel --url http://localhost:3123`
- [x] Named Tunnel mode: spawn `cloudflared tunnel run --token $TUNNEL_TOKEN`
- [x] Parse public URL from cloudflared stderr (Quick Tunnel outputs it during startup)
- [x] Store public URL in module-level variable for other code to read
- [x] Return the public URL (or None if disabled)
- **Assignee:** Merlin
- **Depends on:** 1.2
- **Validation:** Unit tests (test_tunnel.py) — URL parsing, state management, stop/cleanup

### 1.4 Integrate tunnel into merlin.py startup
- [x] Start tunnel in `on_ready()` alongside dashboard and cron scheduler
- [x] Log public URL with `[tunnel]` prefix
- [x] Log clear message when tunnel is disabled
- **Assignee:** Merlin
- **Depends on:** 1.3
- **Validation:** Tunnel logger configured, `_start_tunnel_task()` launched in `on_ready()`

## Phase 2: Authentication

### 2.1 Cookie-based auth middleware
- [x] Create auth middleware for FastAPI (replaces Basic Auth)
- [x] Check `session` cookie on every request
- [x] Valid cookie (HMAC signature checks out, not expired) → proceed
- [x] Invalid/missing cookie → redirect to `/login`
- [x] Exempt routes: `/login`, `/static/*`
- [x] Cookie signing: `hmac(sha256, DASHBOARD_PASS, username + expiry)`
- [x] When `DASHBOARD_PASS` is empty: no auth required (local-only mode)
- **Assignee:** Merlin
- **Depends on:** —
- **Validation:** 27 unit tests in test_auth.py — signing, verification, expiry, tamper detection, cookie set/clear, WebSocket auth

### 2.2 Login page
- [x] `GET /login` — renders login form (dark theme, matching dashboard style)
- [x] `POST /login` — validates password, sets signed cookie, redirects to original URL
- [x] Password field with browser autofill support (`autocomplete="current-password"`)
- [x] Error message on wrong password (inline, no page reload if possible)
- [x] `GET /logout` — clears cookie, redirects to `/login`
- [x] Remember the originally requested URL (redirect back after login)
- **Assignee:** Merlin
- **Depends on:** 2.1
- **Validation:** Login page template with dark theme, `?next=` redirect support, logout link in sidebar

### 2.3 Migrate existing auth
- [x] Remove Basic Auth (`HTTPBasic`, `HTTPBasicCredentials`) from all routes
- [x] Remove `verify_page_auth` dependency from dashboard.py and terminal routes
- [x] Update WebSocket auth: use session cookie instead of query param token
- [x] Remove `ws_token` from template context and `?token=` param from frontend JS
- [x] Update tests to use cookie-based auth
- **Assignee:** Merlin
- **Depends on:** 2.1, 2.2
- **Validation:** All 376 tests pass; Basic Auth completely removed from dashboard.py, terminal/routes.py, terminal.html

### 2.4 Auto-generate password when tunnel is active
- [x] On startup: if tunnel is active and `DASHBOARD_PASS` is empty, generate a random password
- [x] Use `secrets.token_urlsafe(12)` (short enough to type on phone)
- [x] Set the generated password as the active `DASHBOARD_PASS`
- [x] Log credentials clearly: `[bot] Login: admin / <password>`
- [x] Optionally send credentials via Discord (if bot is connected)
- **Assignee:** Merlin
- **Depends on:** 1.4, 2.1
- **Validation:** Password auto-generated and set in env when tunnel active + no DASHBOARD_PASS

## Phase 3: Resilience

### 3.1 Tunnel crash detection and restart
- [x] Monitor cloudflared subprocess (check if process is alive)
- [x] On crash: log error, wait with backoff, restart tunnel
- [x] Max restart attempts before giving up (log warning, continue without tunnel)
- [x] Clean shutdown: kill cloudflared on Merlin exit
- **Assignee:** Merlin
- **Depends on:** 1.4
- **Validation:** Unit tests for stop_tunnel (terminate, kill fallback); exponential backoff in start_tunnel

### 3.2 Tunnel status in dashboard
- [x] Add tunnel status to health API (`/api/health`): enabled, URL, status
- [x] Show public URL somewhere visible (overview page)
- [x] Copy-to-clipboard button for the URL
- **Assignee:** Merlin
- **Depends on:** 1.4
- **Validation:** Health API returns `tunnel_url` and `tunnel_status`; overview card shows tunnel info

## Phase 4: Validation

### 4.1 End-to-end test
- [x] Start Merlin with Quick Tunnel → access dashboard via generated URL
- [x] Verify HTTPS (curl confirms HTTPS, Cloudflare TLS termination)
- [x] Login via login page, verify cookie is set (HMAC-signed, Secure flag)
- [x] Test terminal via tunnel URL (WebSocket through tunnel — connected, tmux session active)
- [x] Verify open redirect protection through tunnel
- [x] All dashboard pages load correctly (overview, logs, performance, terminal)
- [x] Tunnel card shows active status with copy-to-clipboard button
- **Note:** Voice transcription not tested (requires real microphone input from mobile browser)
- **Assignee:** Merlin
- **Depends on:** all above
- **Validation:** 12 curl tests + 8 Playwright screenshots (mobile + desktop) via HTTPS tunnel
