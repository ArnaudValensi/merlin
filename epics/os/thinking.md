# OS Epic — Thinking

## The Insight

The project started as Merlin — a Discord AI assistant. But the real value that emerged is the **development environment**: file browser, web terminal, commit viewer, notes editor, auth + tunnel. These tools have nothing to do with a Discord bot. They're a **portable mobile dev environment**.

The idea: **invert the architecture**. The core product is the dev environment. Merlin becomes just one app within it.

## What We're Building

A tool you can launch on any Linux machine that gives you a mobile-friendly dev environment accessible from anywhere.

```
$ cd ~/my-project
$ uv run ~/merlin/main.py
→ Starts web server on port 3123
→ Creates Cloudflare tunnel
→ Prints URL
→ You open it on your phone
→ You can browse files, use a terminal, view git history, edit notes
```

No Discord. No bot. No cron system. Just a dev environment.

## Decisions Made

### Naming: **Merlin**
Keep "Merlin" as the project name. The bot becomes "Merlin Bot" (already `merlin-bot/`). The dev environment is just Merlin.

### CWD: **Option 1 — launch directory**
v1: CWD = where you launch `main.py`. No switching from the UI. Simple and functional. UI switching is a future enhancement.

### Entry point: **`main.py`**
At project root. Starts FastAPI, auth, tunnel. `merlin.py` stays as the Discord bot inside `merlin-bot/`.

### Core modules: **move to project root**
`files/`, `terminal/`, `commits/`, `notes/` come out of `merlin-bot/` and live at the project root alongside `main.py`.

### Design system: **keep everything**
Same `dashboard.css`, `dashboard.js`, `base.html`, same auth, same tunnel, same dark theme. No redesign.

### Merlin Bot: **plugs in via app mechanism**
`merlin-bot/` exports a router with its monitoring pages (Overview, Performance, Logs, Session). `main.py` checks if `merlin-bot/` exists and has the right exports, and if so, registers the routes and adds nav items to the sidebar. Simple if-statement for v1.

## Current State Analysis

### What moves to project root

| File/Dir | From | Purpose |
|----------|------|---------|
| `files/` | `merlin-bot/files/` | File browser module |
| `terminal/` | `merlin-bot/terminal/` | Web terminal module |
| `commits/` | `merlin-bot/commits/` | Commit browser module |
| `notes/` | `merlin-bot/notes/` | Notes editor module |
| `auth.py` | `merlin-bot/auth.py` | Cookie-based auth |
| `tunnel.py` | `merlin-bot/tunnel.py` | Cloudflare tunnel |
| `static/dashboard.css` | `merlin-bot/static/dashboard.css` | Design system |
| `static/dashboard.js` | `merlin-bot/static/dashboard.js` | Shared JS utilities |
| `templates/base.html` | `merlin-bot/templates/base.html` | Layout shell |
| `templates/login.html` | `merlin-bot/templates/login.html` | Auth page |

### What stays in merlin-bot/

| File | Purpose |
|------|---------|
| `merlin.py` | Discord bot (no longer starts dashboard) |
| `claude_wrapper.py` | Claude Code invocation |
| `discord_send.py` | Discord REST API |
| `cron_runner.py`, `cron_manage.py`, `cron_state.py` | Cron system |
| `session_registry.py` | Discord session management |
| `structured_log.py` | JSONL event logger |
| `memory_search.py`, `kb_add.py`, `remember.py` | Memory system |
| `transcribe.py` | Voice transcription |
| `templates/overview.html` | Merlin monitoring page |
| `templates/performance.html` | Merlin monitoring page |
| `templates/logs.html` | Merlin monitoring page |
| `templates/session.html` | Merlin session viewer |
| `memory/` | Knowledge base data |
| `.env` | Bot credentials |
| `CLAUDE.md` | Bot personality |

### What's new at project root

| File | Purpose |
|------|---------|
| `main.py` | Entry point — FastAPI + auth + tunnel + app discovery |

### Current coupling points to fix

1. **Module path references**: Each module does `MERLIN_BOT_DIR = Path(__file__).parent.parent.resolve()` to find `templates/base.html`. After move, this becomes project root — simpler.

2. **Notes MEMORY_DIR**: Currently `merlin-bot/memory/`. Needs to become configurable or relative to CWD.

3. **Sidebar nav**: Hardcoded in `base.html` with all 7 items. Needs to become dynamic — core items (Notes, Commits, Files, Terminal) always shown, Merlin items (Overview, Performance, Logs) only when the app is loaded.

4. **dashboard.py core routes**: The health API, events API, invocations API, jobs API are Merlin-specific. The auth routes (login/logout) are core. Need to split.

5. **`dashboard.js` bot status**: `updateBotStatus()` checks if Merlin bot is online. This is Merlin-specific — should only run when the app is loaded.

6. **merlin.py startup**: Currently starts dashboard + tunnel + cron. After split, it only starts the bot + cron. Dashboard is started by `main.py`.
