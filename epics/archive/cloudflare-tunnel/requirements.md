# Epic: Cloudflare Tunnel — Public HTTPS Access

## Problem

Merlin's dashboard runs on a local port (3123) over HTTP. This causes two issues:

1. **No mic/voice on mobile** — `navigator.mediaDevices.getUserMedia` requires HTTPS (secure context). Voice transcription in the terminal UI is broken on phones accessing via HTTP.
2. **Difficult remote access** — Accessing Merlin from outside the local network requires port forwarding, firewall rules, and dynamic DNS. This is a barrier for users deploying on a Raspberry Pi or cloud VM.

## Solution

Integrate `cloudflared` (Cloudflare Tunnel) to automatically expose the dashboard over HTTPS with zero network configuration. Replace Basic Auth with cookie-based login for better UX.

Two tunnel modes:

- **Quick Tunnel (default)** — No account, no domain, no config. Generates a random `https://<random>.trycloudflare.com` URL instantly. URL changes on restart.
- **Named Tunnel (optional)** — User provides Cloudflare token + domain in `.env`. Gets a stable subdomain like `merlin.example.com`. Requires a Cloudflare account and domain.

## Goals

- **Zero-config HTTPS** — Quick Tunnel works out of the box with no `.env` variables
- **Minimal onboarding** — Run Docker → see public URL → open on phone
- **Voice works on mobile** — HTTPS enables `getUserMedia` for transcription
- **Secure by default** — Auth is enforced when tunnel is active
- **Fail-fast** — `cloudflared` is a hard dependency; Merlin refuses to start if it's missing
- **Login once** — Cookie-based auth replaces Basic Auth; no more browser prompts

## Non-Goals (for now)

- UI-based configuration of tunnel/auth settings (future epic)
- Custom domain management in the dashboard
- Multi-user access control
- Changing credentials from the UI

## Authentication

### Replace Basic Auth with cookie-based login page

Basic Auth has poor UX: browsers re-prompt on every session, no "remember me", ugly native dialog. Replace with a cookie-based login flow.

#### Flow

1. User opens any page (e.g., `/terminal`)
2. Server middleware checks: does the request have a valid `session` cookie?
3. **No cookie / invalid** → redirect to `/login`
4. `/login` shows a simple form with password field
5. User submits password → server verifies against `DASHBOARD_PASS`
6. **Match** → server responds with `Set-Cookie: session=<signed-value>; Max-Age=2592000; HttpOnly; Secure; SameSite=Lax`
7. Browser stores the cookie automatically and redirects to the originally requested page
8. All subsequent requests: browser sends `Cookie: session=...` automatically — no JavaScript needed
9. Server validates the HMAC signature on the cookie → valid → serve the page

#### Cookie details

- **Signing**: HMAC-SHA256 with `DASHBOARD_PASS` as the key. Value contains username + expiry timestamp.
- **Survives restarts**: same password = same signing key. Cookie remains valid.
- **Invalidated on password change**: new password = new key = old cookies rejected.
- **No server-side storage**: the cookie is self-contained (signed, not encrypted). No session database needed.
- **Flags**: `HttpOnly` (JS can't read it, XSS-safe), `Secure` (HTTPS only, skipped on localhost), `SameSite=Lax` (CSRF protection).
- **Expiry**: 30 days. After that, re-login.
- **GDPR**: auth cookies are "strictly necessary" — no consent banner needed.

#### Domain change (Quick Tunnel)

Cookies are domain-scoped. With Quick Tunnel, the domain changes each restart, so the cookie is lost. User must re-login once per restart (via a clean login form with password autofill). With Named Tunnel (stable domain), the cookie persists across restarts.

#### WebSocket auth

The terminal WebSocket currently uses a token in the query param (`?token=base64`). After switching to cookies, the WebSocket will use the same session cookie (browsers send cookies on WebSocket upgrade requests to the same origin). Remove the query param approach.

### Logout

`/logout` endpoint clears the cookie and redirects to `/login`.

## Configuration

All via `.env` (or environment variables):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TUNNEL_ENABLED` | No | `true` | Enable/disable tunnel. Set `false` to disable. |
| `TUNNEL_TOKEN` | No | — | Cloudflare Tunnel token for named tunnel mode. If unset, uses Quick Tunnel. |
| `TUNNEL_HOSTNAME` | No | — | Custom hostname (e.g., `merlin.example.com`). Only used with `TUNNEL_TOKEN`. |
| `DASHBOARD_USER` | No | `admin` | Login username |
| `DASHBOARD_PASS` | No* | auto-generated | *Auto-generated when tunnel is active and not set. |

## Security Requirements

- **Fail-fast on missing cloudflared** — Merlin checks for `cloudflared` binary at startup (in `_validate_config()`). If not found, exit with clear install instructions. Hard dependency.
- **Mandatory auth when public** — If tunnel is active and `DASHBOARD_PASS` is empty, auto-generate a random password at startup, log it clearly, and send it via Discord if the bot is configured.
- **No open dashboard on the internet** — All routes require auth when `DASHBOARD_PASS` is set.
- **Terminal access = shell access** — Emphasize this in logs when tunnel starts.
- **Cookie security** — `HttpOnly`, `Secure` (when HTTPS), `SameSite=Lax`, HMAC-signed.

## User Experience

### First run (zero config)
```
$ docker run merlin
[bot]       Starting Merlin...
[tunnel]    Quick Tunnel starting (no TUNNEL_TOKEN configured)
[tunnel]    Dashboard available at: https://abc123-random.trycloudflare.com
[tunnel]    Auto-generated password: xK9m#2pL (save this!)
[tunnel]    Login: admin / xK9m#2pL
[bot]       Bot ready as Merlin#0000
```

### With named tunnel
```
$ docker run -e TUNNEL_TOKEN=eyJ... -e TUNNEL_HOSTNAME=merlin.example.com -e DASHBOARD_PASS=mysecret merlin
[bot]       Starting Merlin...
[tunnel]    Named Tunnel connecting to merlin.example.com
[tunnel]    Dashboard available at: https://merlin.example.com
[bot]       Bot ready as Merlin#0000
```

### Tunnel disabled
```
$ docker run -e TUNNEL_ENABLED=false merlin
[bot]       Starting Merlin...
[bot]       Dashboard starting on http://0.0.0.0:3123
[bot]       Tunnel disabled — local access only
```

### Login flow (user perspective)
1. Open `https://abc123.trycloudflare.com/terminal` on phone
2. Redirected to `/login` — clean form, one password field
3. Browser offers to autofill saved password → submit
4. Cookie set → redirected to `/terminal`
5. All subsequent pages work without re-login (until cookie expires or domain changes)

## Technical Notes

- `cloudflared` is a single Go binary (~30MB). Install in Dockerfile via official release.
- Quick Tunnel: `cloudflared tunnel --url http://localhost:3123` — parses URL from stderr.
- Named Tunnel: `cloudflared tunnel run --token <TOKEN>` — hostname configured in Cloudflare dashboard.
- Tunnel process managed like cron scheduler — started in `on_ready`, monitored, restarted on crash.
- Public URL stored in memory for display in dashboard UI and Discord.
- Cookie signing: `hmac(sha256, DASHBOARD_PASS, username + expiry)` — no extra secret to manage.
- Login page must be clean, minimal, match the dashboard dark theme.

## Architecture

```
merlin.py _validate_config()
└── check cloudflared binary exists → fail-fast if missing

merlin.py on_ready()
├── start dashboard (uvicorn on 0.0.0.0:3123)
├── start cron scheduler
└── start tunnel (new)
    ├── Quick Tunnel: cloudflared tunnel --url localhost:3123
    │   └── parse URL from stderr → log + store
    └── Named Tunnel: cloudflared tunnel run --token $TUNNEL_TOKEN
        └── log hostname from config

Dashboard request flow:
  Request → cookie auth middleware
    ├── /login, /static → public (no auth)
    ├── valid session cookie → proceed to route
    └── no/invalid cookie → redirect to /login
```

## Acceptance Criteria

- [ ] `cloudflared` installed in Docker image
- [ ] Fail-fast: Merlin exits with clear message if `cloudflared` not found
- [ ] Quick Tunnel starts automatically with no config, URL printed in logs
- [ ] Named Tunnel works when `TUNNEL_TOKEN` is set
- [ ] Tunnel can be disabled with `TUNNEL_ENABLED=false`
- [ ] Auto-generated password when tunnel is active and `DASHBOARD_PASS` is empty
- [ ] Password displayed clearly in startup logs
- [ ] Login page replaces Basic Auth — clean form, dark theme, password autofill works
- [ ] Signed session cookie (30 day expiry), survives Merlin restarts
- [ ] All routes require auth (redirect to /login if no valid cookie)
- [ ] WebSocket auth via cookie (remove query param token)
- [ ] Logout endpoint clears cookie
- [ ] Tunnel crash detection and auto-restart
- [ ] Voice transcription works on mobile via HTTPS tunnel URL
