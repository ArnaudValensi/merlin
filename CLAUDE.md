# Merlin — Project

Merlin is a portable mobile dev environment. Install via `curl | bash` or run from a git checkout with `uv run main.py`. Provides a web-based development environment (file browser, terminal, git viewer, notes editor) accessible from anywhere via Cloudflare tunnel.

Merlin Bot (Discord AI assistant) is an optional app that plugs into the system.

## Project Structure

```
merlin/
├── CLAUDE.md                  # This file — development instructions
├── main.py                    # FastAPI app — dashboard + tunnel + bot + cron
├── cli.py                     # CLI entry point (merlin start/version/setup/update)
├── paths.py                   # Path resolution (dev mode vs installed mode)
├── install.sh                 # curl|bash installer
├── auth.py                    # Cookie-based auth (HMAC-signed)
├── tunnel.py                  # Cloudflare Tunnel manager
├── static/
│   ├── dashboard.css          # Design system (dark theme, CSS variables)
│   └── dashboard.js           # Shared JS (API, refresh, formatting)
├── templates/
│   ├── base.html              # Layout shell (dynamic sidebar)
│   └── login.html             # Auth page
├── files/                     # File browser module
├── terminal/                  # Web terminal module (xterm.js + tmux)
├── commits/                   # Git commit browser module
├── notes/                     # Notes editor module (markdown)
├── tests/                     # Tests for core modules
├── merlin-bot/                # Merlin Bot plugin (optional)
│   ├── CLAUDE.md              # Bot personality and directives
│   ├── merlin_bot.py           # Discord bot + cron + plugin interface
│   ├── merlin_app.py          # App interface (monitoring pages for dashboard)
│   ├── claude_wrapper.py      # Single entry point for all Claude Code calls
│   ├── discord_send.py        # Discord REST API (send, reply, react)
│   ├── cron_runner.py         # Cron job dispatcher
│   ├── cron_manage.py         # Cron job management CLI
│   ├── cron_state.py          # Cron state/history helpers
│   ├── cron-jobs/             # Job files (*.json)
│   ├── merlin_app.py          # App interface (monitoring pages for dashboard)
│   ├── templates/             # Bot-specific templates (overview, performance, logs)
│   ├── .claude/
│   │   ├── settings.json      # Hooks config
│   │   ├── hooks/             # PreToolUse hooks
│   │   └── skills/            # Skills (discord/, cron/)
│   ├── .env                   # Bot token (gitignored)
│   ├── tests/                 # Bot-specific tests
│   └── logs/                  # Invocation logs (gitignored)
├── epics/                     # Project management
│   ├── <epic-name>/           # Active epics
│   └── archive/               # Completed epics
├── docs/                      # Reference documentation (see list below)
```

### Reference Documentation

Read these docs when working on the corresponding systems:

| Doc | Covers |
|-----|--------|
| [`docs/architecture.md`](docs/architecture.md) | High-level system overview, data flow |
| [`docs/cron-system.md`](docs/cron-system.md) | Job format, dispatcher, state/locks, scheduler, staleness guard |
| [`docs/memory-system.md`](docs/memory-system.md) | 3-layer memory (user, logs, KB), frontmatter format, search tools |
| [`docs/session-management.md`](docs/session-management.md) | Session registry, UUID5 strategy, resume-first, MERLIN_SESSION_ID |
| [`docs/discord-bot.md`](docs/discord-bot.md) | Message flow, filtering, threading, prompt building, discord skill |
| [`docs/auth-and-tunnel.md`](docs/auth-and-tunnel.md) | Cookie auth, HMAC signing, Cloudflare Tunnel modes, login flow |
| [`docs/web-terminal.md`](docs/web-terminal.md) | xterm.js, WebSocket, PTY/tmux, mobile toolbar, voice input |
| [`docs/session-viewer.md`](docs/session-viewer.md) | Session transcripts, stream-json format, timeline rendering |
| [`docs/notes-editor.md`](docs/notes-editor.md) | Notes routes, command palette, git ops, media upload, content search |
| [`docs/dashboard-architecture.md`](docs/dashboard-architecture.md) | Dashboard theme, CSS variables, JS patterns, API endpoints |
| [`docs/claude-code-reference.md`](docs/claude-code-reference.md) | Claude Code CLI flags and options |
| [`docs/releasing.md`](docs/releasing.md) | Tagging, GitHub Releases, install/update flow, rollback |
| [`docs/standalone-cli.md`](docs/standalone-cli.md) | Standalone CLI design: paths, install, update, dev mode |

### Two CLAUDE.md Files

1. **`merlin/CLAUDE.md`** (this file) — For **developing** Merlin
2. **`merlin-bot/CLAUDE.md`** — For **running as** Merlin (personality, directives)

## Script Documentation Convention

**All scripts are self-documented via `--help`.** To understand any script:

```bash
uv run main.py --help              # Core entry point
cd merlin-bot && uv run <script>.py --help  # Bot scripts
```

When creating new scripts:
- Use argparse with descriptive help text for all options
- Include usage examples in the epilog
- Document output format and common use cases
- Keep this CLAUDE.md concise — point to `--help` for details

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                        main.py                            │
│    (FastAPI + auth + tunnel + bot plugin — one process)    │
│                                                           │
│  Core Modules:                                            │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────┐    │
│  │  files/   │ │terminal/ │ │ commits/ │ │  notes/    │    │
│  │  Browser  │ │ xterm.js │ │ Git log  │ │ Markdown   │    │
│  └──────────┘ └──────────┘ └──────────┘ └───────────┘    │
│                                                           │
│  Bot Plugin (optional — merlin-bot/):                     │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  merlin_app.py — monitoring pages + API endpoints   │  │
│  │  merlin_bot.py — Discord client + cron scheduler    │  │
│  └─────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
                          │
               ┌──────────┼──────────┐
               │          │          │
               ▼          ▼          ▼
          Cloudflare   Browser    Discord
           Tunnel    (mobile/      API
                     desktop)
```

### Components

**Core (project root):**

| File | Purpose | Docs |
|------|---------|------|
| `main.py` | FastAPI app — starts dashboard + tunnel + bot + cron (one process) | `--help` |
| `cli.py` | CLI entry point — `merlin start/version/setup/update` | `--help`, [`standalone-cli`](docs/standalone-cli.md) |
| `paths.py` | Path resolution — dev mode vs installed mode (`~/.merlin/`) | [`standalone-cli`](docs/standalone-cli.md) |
| `install.sh` | `curl \| bash` installer | [`releasing`](docs/releasing.md) |
| `auth.py` | Cookie-based HMAC auth | [`auth-and-tunnel`](docs/auth-and-tunnel.md) |
| `tunnel.py` | Cloudflare Tunnel manager | [`auth-and-tunnel`](docs/auth-and-tunnel.md) |
| `files/` | File browser module | [`dashboard-architecture`](docs/dashboard-architecture.md) |
| `terminal/` | Web terminal module | [`web-terminal`](docs/web-terminal.md) |
| `commits/` | Commit browser module | [`dashboard-architecture`](docs/dashboard-architecture.md) |
| `notes/` | Notes editor module | [`notes-editor`](docs/notes-editor.md) |

**Merlin Bot plugin (merlin-bot/):**

| Script | Purpose | Docs |
|--------|---------|------|
| `merlin_bot.py` | Discord bot + cron + plugin interface (router, start, validate) | [`discord-bot`](docs/discord-bot.md), [`cron-system`](docs/cron-system.md) |
| `merlin_app.py` | App interface for dashboard (monitoring pages) | [`dashboard-architecture`](docs/dashboard-architecture.md) |
| `claude_wrapper.py` | Single entry point for Claude calls | `--help`, [`session-management`](docs/session-management.md) |
| `discord_send.py` | Send/reply/react to Discord | `--help`, [`discord-bot`](docs/discord-bot.md) |
| `cron_runner.py` | Cron job dispatcher | `--help`, [`cron-system`](docs/cron-system.md) |
| `cron_manage.py` | Manage cron jobs | `--help`, [`cron-system`](docs/cron-system.md) |
| `memory_search.py` | Search KB, logs, and list tags | `--help`, [`memory-system`](docs/memory-system.md) |
| `kb_add.py` | Add KB entries with auto-linking | `--help`, [`memory-system`](docs/memory-system.md) |
| `remember.py` | Add user facts to user.md | `--help`, [`memory-system`](docs/memory-system.md) |

### CWD (Current Working Directory)

The CWD is determined by where you launch `main.py`:
- File browser defaults to CWD (can navigate anywhere)
- Commits show the CWD's git repo
- Terminal starts in CWD
- Notes use `merlin-bot/memory/` (not CWD-relative yet)

### Session Management

> Full reference: [`docs/session-management.md`](docs/session-management.md)

Every conversation lives in a **Discord thread**, mapped 1:1 to a Claude Code session.

- **Channel message** → creates a thread, generates session via `uuid5("discord-thread-{thread_id}")`
- **Thread message** → looks up session from `data/session_registry.json`, resumes it
- **Reply to cron/bot message** → resumes the cron's session (tracked via `MERLIN_SESSION_ID` env var)

Strategy: **resume-first** — try `--resume` first, fall back to `--session-id` to create.

## Tech Stack

- **Language**: Python
- **Runner**: `uv run` (PEP 723 inline dependencies)
- **Web**: FastAPI + Jinja2 (server-side rendered)
- **LLM**: Claude Code CLI (`claude -p`)
- **Discord**: discord.py (listener) + httpx (REST API)
- **Scheduling**: Built-in asyncio scheduler (no cron dependency)

## Discord

> Full reference: [`docs/discord-bot.md`](docs/discord-bot.md)

- **Bot token**: `merlin-bot/.env` (see `.env.example`)
- **Default channel**: Set via `DISCORD_CHANNEL_IDS` in config
- **Script**: `uv run discord_send.py --help`

## Cron Jobs

> Full reference: [`docs/cron-system.md`](docs/cron-system.md)

- **Job files**: `merlin-bot/cron-jobs/*.json`
- **Management**: `uv run cron_manage.py --help`
- **Dispatcher**: Built into `merlin_bot.py` (runs `cron_runner.py` every minute)
- **Monitoring**: Crashes logged to `structured.jsonl` and alerted via Discord

## Environment

- **OS**: Arch Linux (Docker)
- **Package Manager**: pacman
- **Available Ports**: 3123, 3124, 3125

## Development Commands

```bash
# Start everything (dashboard + bot + cron, no tunnel)
uv run main.py --no-tunnel       # Direct (dev mode)
uv run cli.py start --no-tunnel  # Via CLI entry point
merlin start --no-tunnel          # If installed

# Restart everything (single process, background)
restart.sh   # or just `merlin` (shell alias)

# Run core tests
merlin-bot/.venv/bin/pytest tests/ --ignore=tests/test_touch_scroll.py -v

# Run bot tests
cd merlin-bot && .venv/bin/pytest tests/ -v

# First-time test setup
cd merlin-bot && uv venv .venv && uv pip install --python .venv/bin/python pytest croniter
```

## Logging

All Claude invocations go through `claude_wrapper.py`, which logs:
- Prompt, full stdout/stderr, exit code, duration, usage stats
- One file per invocation: `logs/claude/<timestamp>-<caller>-<session>.log`

**Structured log** (`logs/structured.jsonl`):
- Single JSONL file, one JSON line per event
- Event types: `invocation`, `bot_event`, `cron_dispatch`
- Source of truth for the monitoring dashboard

Additional text logs:
- `logs/merlin.log` — Discord bot events
- `logs/cron_runner.log` — Dispatcher activity

## Monitoring Dashboard

> Full reference: [`docs/dashboard-architecture.md`](docs/dashboard-architecture.md) | Auth & tunnel: [`docs/auth-and-tunnel.md`](docs/auth-and-tunnel.md)

Web-based dashboard served by FastAPI on port 3123, started by `main.py`.

- **Auth:** Cookie-based auth (`DASHBOARD_USER` / `DASHBOARD_PASS` in `.env`) — see [`docs/auth-and-tunnel.md`](docs/auth-and-tunnel.md)
- **Core pages:** Files, Terminal, Commits, Notes (always available)
- **Bot pages:** Overview, Performance, Logs (available when merlin-bot is present)
- **Start:** `uv run main.py` starts everything (dashboard + bot + cron) in one process
- **Screenshots:** `uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --user admin --pass <pass>`

## Project Management

Work is organized into **epics** under `epics/`. Each epic contains:
- `requirements.md` — Goals and acceptance criteria
- `tasks.md` — Concrete tasks with status, assignee, dependencies
- `journal/` — Timestamped work logs for context restoration

### Workflow

1. Discuss → 2. Create epic → 3. Plan tasks → 4. Work (with journal entries) → 5. Validate → 6. Archive

### Validation

Every task needs validation before marking done:
- Preferred: pytest unit tests
- Alternative: smoke tests, integration scripts, dry-run verification
- Document validation method in journal entry

## Key Patterns and Conventions

- **PEP 723**: Inline dependencies in each script
- **Self-documenting scripts**: Comprehensive `--help` with examples
- **Single Claude entry point**: Always use `claude_wrapper.py`, never `claude` directly
- **Deterministic sessions**: UUID5 from channel/job ID for memory persistence
- **App plugin pattern**: `merlin_bot.py` exports `router`, `NAV_ITEMS`, `STATIC_DIR`, plus `start()`, `on_tunnel_url()`, `validate()`. `main.py` discovers via `import merlin_bot` (merlin-bot/ is on sys.path) and runs everything in one process.
- **Dynamic sidebar**: Nav items are template variables, not hardcoded. Core items always shown, app items added when app is loaded.
- **Path resolution (paths.py)**: All modules use `paths.py` for file/directory resolution. Only `app_dir()` differs between modes (repo root vs `~/.merlin/current/`). User data (memory, cron-jobs, logs, config) always lives under `~/.merlin/` regardless of mode. Dev mode detection: explicit `set_dev_mode()` > `MERLIN_DEV` env var > `.git/` directory presence. Custom install location via `MERLIN_HOME` env var.
- **Graceful degradation**: At startup, `_check_optional_deps()` checks for tmux and cloudflared. Missing deps result in boot warnings, disabled nav items (grayed out with tooltip), and 503 responses on affected routes — not crashes.
- **Fail-fast configuration**: All entry points (`merlin_bot.py`, `cron_runner.py`) validate required config at startup and exit immediately with descriptive error messages and step-by-step setup instructions if anything is missing or invalid. A first-time user should see exactly what to do — never a cryptic crash later at runtime. When adding new required config, always add validation to the entry point's `_validate_config()` function.
- **Web UI development**: Before making any dashboard or UI changes, read `docs/dashboard-architecture.md` for theme variables, CSS conventions, JS patterns, API endpoints, and how to add new pages. Always self-validate UI changes by taking screenshots with the screenshot skill and reviewing them before marking work as done. Run `uv run .claude/skills/screenshot/screenshot.py --all <url> --user <user> --pass <pass>` from the project root, then read the PNGs to verify layout, responsiveness, and correctness across viewports.
