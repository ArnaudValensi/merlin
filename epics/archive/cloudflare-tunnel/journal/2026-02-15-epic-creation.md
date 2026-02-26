# Journal: Cloudflare Tunnel Epic — Creation & Context

**Date:** 2026-02-15

## Origin

This epic emerged from the web-terminal epic (now archived at `epics/archive/web-terminal/`). The terminal UI works fully — PTY via WebSocket, xterm.js with Dracula theme, mobile toolbar with modifier/function/arrow keys + Shift+Tab, voice transcription, touch scroll via SGR mouse escape sequences, auto-reconnect. But voice input (`navigator.mediaDevices.getUserMedia`) requires HTTPS, which the dashboard doesn't have.

The user accesses Merlin from their iPhone with Brave browser. The dashboard runs on port 3123 over HTTP inside Docker. They want:
- HTTPS so voice works on mobile
- Easy remote access without port forwarding
- Minimal setup for new Merlin deployments (Raspberry Pi scenario)
- Better auth UX than Basic Auth (which re-prompts every session)

## Decisions Made

### Cloudflare Tunnel
- **Quick Tunnel** is the default (zero config): `cloudflared tunnel --url localhost:3123` gives a random `*.trycloudflare.com` HTTPS URL. No account needed.
- **Named Tunnel** is optional: set `TUNNEL_TOKEN` + `TUNNEL_HOSTNAME` in `.env` for a stable subdomain.
- `cloudflared` is a **hard dependency** — fail-fast in `_validate_config()` if not found. Not optional.
- `TUNNEL_ENABLED=false` to disable (for local-only dev).

### Cookie Auth (replaces Basic Auth)
- User chose **cookies over JWT** because Merlin serves server-rendered HTML (Jinja templates). Cookies are sent automatically by the browser on every request including page navigation. JWT/bearer tokens would require JavaScript on every page to attach the header, effectively requiring a SPA rewrite.
- **Cookie over URL tokens** because URL tokens leak via browser history, referer headers, and screenshots. With HTTPS the URL path is encrypted in transit, but other leak vectors remain.
- **HMAC-signed cookie** using `DASHBOARD_PASS` as the signing key. No server-side session storage needed. Cookie survives Merlin restarts (same password = same key). Invalidated automatically when password changes.
- **Cookie flags**: `HttpOnly` (XSS protection), `Secure` (HTTPS only, skip on localhost), `SameSite=Lax` (CSRF protection). 30-day expiry.
- **Quick Tunnel limitation**: cookies are domain-scoped. Domain changes each restart → cookie lost → re-login once per restart. With Named Tunnel (stable domain), cookie persists. User understands and accepts this tradeoff.
- **GDPR**: auth cookies are "strictly necessary" — no consent banner needed.
- **Login page**: clean form, dark theme matching dashboard, password autofill support. `/login` (public), `/logout` clears cookie.
- **WebSocket auth migration**: currently uses `?token=base64` query param. After migration, WebSocket uses the session cookie (browsers send cookies on WebSocket upgrade to same origin). Remove the query param approach.

### Auto-generated password
- When tunnel is active and `DASHBOARD_PASS` is empty, generate with `secrets.token_urlsafe(12)` and log it clearly at startup.

## Current State

- **Epic files**: `epics/cloudflare-tunnel/requirements.md` and `tasks.md`
- **No code written yet** — epic is in planning/approved state
- **4 phases, 10 tasks** — infrastructure (1.1-1.4), authentication (2.1-2.4), resilience (3.1-3.2), validation (4.1)

## Key Files That Will Be Modified

| File | Change |
|------|--------|
| `Dockerfile` | Add `cloudflared` binary |
| `merlin.py` | Add `cloudflared` check in `_validate_config()`, start tunnel in `on_ready()` |
| `tunnel.py` (new) | Tunnel manager: start/stop/monitor cloudflared subprocess, parse URL |
| `dashboard.py` | Replace Basic Auth middleware with cookie auth, add `/login` + `/logout` routes |
| `terminal/routes.py` | Remove Basic Auth deps, use cookie auth, remove `ws_token` query param |
| `terminal/templates/terminal.html` | Remove `WS_TOKEN` / `?token=` JS code |
| `templates/login.html` (new) | Login page template |
| `templates/base.html` | Maybe add logout link |
| `tests/test_terminal.py` | Update auth tests from Basic Auth to cookie |
| `.env.example` | Add `TUNNEL_*` variables |

## Existing Auth Code to Replace

### dashboard.py
- `DASHBOARD_USER` / `DASHBOARD_PASS` env vars — keep these, reuse for cookie auth
- `HTTPBasic` / `HTTPBasicCredentials` — remove
- `verify_credentials()` dependency — replace with cookie middleware

### terminal/routes.py
- `_get_credentials()` — keep (reads DASHBOARD_USER/PASS)
- `_security = HTTPBasic(auto_error=False)` — remove
- `verify_page_auth()` — replace with cookie check
- `_verify_ws_auth()` — replace: extract cookie from WebSocket headers instead of Authorization header/query param
- `ws_token` passed to template — remove
- `?token=` query param on WebSocket URL — remove

### terminal/templates/terminal.html
- `const WS_TOKEN = '{{ ws_token }}'` (line ~223) — remove
- `const tokenParam = WS_TOKEN ? '?token=' + ...` (line ~583) — remove, WebSocket URL becomes just `ws://host/ws/terminal`
- Voice transcription `Authorization` header (line ~524) — remove, cookie is sent automatically

## Technical Notes

- `cloudflared tunnel --url http://localhost:3123` outputs the URL to stderr, format: `https://random-thing.trycloudflare.com`
- The URL appears in a line like: `| https://random-thing.trycloudflare.com |` — need to parse with regex
- Quick Tunnel needs no authentication/token — it's a free Cloudflare service
- Named Tunnel: `cloudflared tunnel run --token $TUNNEL_TOKEN` — token encodes all config (tunnel ID, hostname, etc.)
- `cloudflared` is ~30MB single static binary, available for linux/amd64 and linux/arm64

## User Preferences (from conversation)

- Prefers simple, minimal changes
- Config via `.env` for now, UI config is a future epic
- Wants onboarding to be as simple as possible: run Docker → get URL → open on phone
- Merlin should have minimal external dependencies
- The user has their own domain on Cloudflare for Named Tunnel use
