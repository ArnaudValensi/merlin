# Epic: OS — Restructure Merlin as a Portable Dev Environment

## Goal

Transform Merlin from a Discord-bot-with-a-dashboard into a **portable mobile dev environment** that can be launched on any Linux machine. Merlin Bot becomes an optional app within the system.

## User Story

As a developer, I want to run a single command on any Linux machine and get a mobile-friendly web-based dev environment (file browser, terminal, git viewer, notes editor) accessible from anywhere via Cloudflare tunnel.

## Acceptance Criteria

### Core
- [ ] `uv run main.py` at the project root starts the dev environment
- [ ] CWD = the directory where `main.py` is launched from
- [ ] FastAPI serves the web UI on port 3123 (configurable)
- [ ] Cookie-based auth protects all routes
- [ ] Cloudflare tunnel provides public access
- [ ] Core modules work independently with no Merlin Bot dependency

### Core Modules (at project root)
- [ ] **File browser** — browse CWD (and full filesystem), syntax highlighting
- [ ] **Terminal** — xterm.js + tmux, mobile toolbar, voice input
- [ ] **Commits** — git log/diff viewer (works when CWD is a git repo)
- [ ] **Notes** — markdown editor, command palette

### Sidebar
- [ ] Core nav items always visible: Notes, Commits, Files, Terminal
- [ ] App nav items appear dynamically when an app is loaded
- [ ] Branding shows "Merlin" (no "Bot" or monitoring-specific language)

### Merlin Bot App
- [ ] `merlin-bot/` continues to work as the Discord bot
- [ ] Monitoring pages (Overview, Performance, Logs, Session) plug into the sidebar as app nav items
- [ ] Merlin Bot APIs (health, events, invocations, jobs) are served under the app's routes
- [ ] `merlin.py` no longer starts the dashboard — it only runs the bot + cron
- [ ] `merlin.py` can optionally connect to the running dashboard (for bot status indicator)

### Unchanged
- [ ] Same design system (dark theme, CSS variables, responsive)
- [ ] Same auth system (cookie-based HMAC)
- [ ] Same tunnel system (Cloudflare)
- [ ] All existing functionality preserved — nothing removed, just reorganized

## Out of Scope (v1)
- CWD switching from the UI (future enhancement)
- Generic app discovery/SDK (v1 uses simple if-statement for merlin-bot)
- New features or pages
- Redesign of any existing UI
