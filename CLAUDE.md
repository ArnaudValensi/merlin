# Merlin — Project

Merlin is a portable mobile dev environment. Install via `curl | bash` or run from a git checkout with `uv run main.py`. Provides a web-based development environment (file browser, terminal, git viewer, notes editor) accessible from anywhere via Cloudflare tunnel.

Merlin Bot (Discord AI assistant) is an optional extension that plugs into the system.

## Project Structure

```
merlin/
├── CLAUDE.md                  # This file — development instructions
├── main.py                    # FastAPI app — dashboard + tunnel + bot + cron
├── cli.py                     # CLI entry point (merlin start/version/setup/update/config)
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
├── lib/                       # Shared libraries
│   ├── claude.py              # Legacy Claude wrapper (backward compat)
│   ├── engine.py              # AgentEngine abstraction + invoke() entry point
│   ├── agent_context.py       # Persona/context composition (layers + recipes)
│   ├── session.py             # JSONL session manager (history, compaction)
│   └── engines/               # Engine implementations
│       ├── claude_code.py     # Claude Code CLI engine (default)
│       └── opencode.py        # OpenCode CLI engine
├── cron/                      # Cron core module (always active)
│   ├── __init__.py            # Scheduler loop (start(), _cron_scheduler)
│   ├── runner.py              # Job dispatcher (check due, execute via lib/engine)
│   ├── state.py               # State/history/lock helpers
│   ├── manage.py              # Job CRUD + CLI for management
│   ├── routes.py              # REST API endpoints + /cron dashboard page
│   ├── schemas.py             # Pydantic models (JobCreate, JobUpdate)
│   ├── logs.py                # Hybrid log storage (individual files + metadata)
│   ├── notify.py              # Notification system (graceful Discord fallback)
│   └── templates/cron.html    # Dashboard page template
├── files/                     # File browser module
├── terminal/                  # Web terminal module (xterm.js + tmux)
├── commits/                   # Git commit browser module
├── notes/                     # Notes editor module (markdown)
│   └── commands/              # merlin notes search / kb / remember commands
├── skills/                    # Core operational skills (cron/, notes/, dashboard/, self-awareness/) — always active, aggregated regardless of the bot
├── tests/                     # Tests for core modules
├── agent/
│   └── MERLIN.md              # Agent brain doc (printed by `merlin agent`)
├── merlin-bot/                # Merlin Bot extension (optional, built-in, Discord-only)
│   ├── merlin_bot.py          # Discord bot + extension interface (EXTENSION_META)
│   ├── merlin_app.py          # App interface (bot monitoring page with tabs)
│   ├── discord_directives.md  # Canonical Discord style overlay
│   ├── discord_send.py        # Discord REST API transport (used by bot + merlin chat)
│   ├── cron-jobs/             # Job files (*.json)
│   ├── templates/             # Bot-specific templates (bot.html with tabs, session.html)
│   ├── skills/                # Bot-gated skills (discord/) — only active when the bot is enabled
│   ├── .env                   # Bot token (gitignored)
│   ├── tests/                 # Bot-specific tests
│   └── logs/                  # Invocation logs (gitignored)
├── (epics managed in merlin-saas repo)
├── docs/                      # User/platform docs (for people using or extending Merlin)
│   └── dev/                   # Contributor docs (for changing Merlin's code; see list below)
```

### Reference Documentation

Read these docs when working on the corresponding systems:

| Doc | Covers |
|-----|--------|
| [`docs/dev/architecture.md`](docs/dev/architecture.md) | High-level system overview, data flow |
| [`docs/dev/cron-system.md`](docs/dev/cron-system.md) | Job format, dispatcher, state/locks, scheduler, staleness guard |
| [`docs/dev/notes-system.md`](docs/dev/notes-system.md) | 3-layer notes system (user, logs, KB), frontmatter format, search tools |
| [`docs/dev/session-management.md`](docs/dev/session-management.md) | Session registry, UUID5 strategy, resume-first, MERLIN_SESSION_ID |
| [`docs/dev/discord-bot.md`](docs/dev/discord-bot.md) | Message flow, filtering, threading, prompt building, discord skill |
| [`docs/dev/auth-and-tunnel.md`](docs/dev/auth-and-tunnel.md) | Cookie auth, HMAC signing, Cloudflare Tunnel modes, login flow |
| [`docs/web-terminal.md`](docs/web-terminal.md) | xterm.js, WebSocket, PTY/tmux, mobile toolbar, voice input |
| [`docs/dev/session-viewer.md`](docs/dev/session-viewer.md) | Session transcripts, stream-json format, timeline rendering |
| [`docs/dev/notes-editor.md`](docs/dev/notes-editor.md) | Notes routes, command palette, git ops, media upload, content search |
| [`docs/dev/extension-system.md`](docs/dev/extension-system.md) | Extension tiers, interface, state, registry, Extensions/Settings pages |
| [`docs/dev/skill-system.md`](docs/dev/skill-system.md) | Skill registry: sources, precedence (core > extension > user), canonical aggregation, engine adapters, shims, `merlin skills` |
| [`docs/dev/dashboard-architecture.md`](docs/dev/dashboard-architecture.md) | Dashboard theme, CSS variables, JS patterns, API endpoints |
| [`docs/dev/claude-code-reference.md`](docs/dev/claude-code-reference.md) | Claude Code CLI flags and options |
| [`docs/dev/releasing.md`](docs/dev/releasing.md) | Tagging, GitHub Releases, install/update flow, rollback |
| [`docs/dev/standalone-cli.md`](docs/dev/standalone-cli.md) | Standalone CLI design: paths, install, update, dev mode |
| [`docs/dev/logging-system.md`](docs/dev/logging-system.md) | Logging architecture, log files, rotation, engine log events |

### Doc Audiences

1. **`merlin/CLAUDE.md`** (this file) — For **developing** Merlin. No operational agent guidance lives here.
2. **`agent/MERLIN.md`** — The agent brain doc: what Merlin is and how to operate it, channel-neutral. Printed by `merlin agent`. Discord style lives in `merlin-bot/discord_directives.md`.

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
│    (FastAPI + auth + tunnel + extensions — one process)    │
│                                                           │
│  Core Modules:                                            │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────┐   │
│  │  files/   │ │terminal/ │ │ commits/ │ │  cron/     │   │
│  │  Browser  │ │ xterm.js │ │ Git log  │ │ Scheduler  │   │
│  └──────────┘ └──────────┘ └──────────┘ └───────────┘   │
│                                                           │
│  Shared Libraries:                                        │
│  ┌──────────────────────────────────────────────────┐    │
│  │  lib/engine.py — AgentEngine (provider-agnostic)   │    │
│  └──────────────────────────────────────────────────┘    │
│                                                           │
│  Built-in Extensions:                                     │
│  ┌───────────┐ ┌─────────────────────────────────────┐   │
│  │  notes/    │ │  merlin-bot/ (optional)             │   │
│  │  Markdown  │ │  merlin_app.py — bot monitoring     │   │
│  └───────────┘ │  merlin_bot.py — Discord only        │   │
│                 └─────────────────────────────────────┘   │
│                                                           │
│  Installed Extensions: ~/.merlin/extensions/               │
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
| `cli.py` | CLI entry point — `merlin start/version/setup/update/config` | `--help`, [`standalone-cli`](docs/dev/standalone-cli.md) |
| `paths.py` | Path resolution — dev mode vs installed mode (`~/.merlin/`) | [`standalone-cli`](docs/dev/standalone-cli.md) |
| `install.sh` | `curl \| bash` installer | [`releasing`](docs/dev/releasing.md) |
| `auth.py` | Cookie-based HMAC auth | [`auth-and-tunnel`](docs/dev/auth-and-tunnel.md) |
| `tunnel.py` | Cloudflare Tunnel manager | [`auth-and-tunnel`](docs/dev/auth-and-tunnel.md) |
| `lib/engine.py` | AgentEngine abstraction — provider-agnostic invocation (`invoke()`) | [`session-management`](docs/dev/session-management.md) |
| `lib/agent_context.py` | Persona/context composition — layers (brain, personality, user, overlays) and per-caller recipes | [`architecture`](docs/dev/architecture.md) |
| `lib/session.py` | Session manager — JSONL transcripts, history, compaction | [`session-management`](docs/dev/session-management.md) |
| `lib/engines/` | Engine implementations (claude_code.py, opencode.py) | [`session-management`](docs/dev/session-management.md) |
| `cron/` | Cron core module — scheduler, runner, state, REST API, logs, notifications | [`cron-system`](docs/dev/cron-system.md) |
| `files/` | File browser module | [`dashboard-architecture`](docs/dev/dashboard-architecture.md) |
| `terminal/` | Web terminal module | [`web-terminal`](docs/web-terminal.md) |
| `commits/` | Commit browser module | [`dashboard-architecture`](docs/dev/dashboard-architecture.md) |
| `notes/` | Notes editor module | [`notes-editor`](docs/dev/notes-editor.md) |

**Merlin Bot extension (merlin-bot/) — Discord-only scope:**

| Script | Purpose | Docs |
|--------|---------|------|
| `merlin_bot.py` | Discord bot + extension interface (router, start, validate, EXTENSION_META) | [`discord-bot`](docs/dev/discord-bot.md) |
| `merlin_app.py` | Bot monitoring page with tabs (Overview, Performance, Logs) | [`dashboard-architecture`](docs/dev/dashboard-architecture.md) |
| `discord_directives.md` | Canonical Discord style overlay | [`discord-bot`](docs/dev/discord-bot.md) |
| `discord_send.py` | Discord REST transport (CLI: `merlin chat`) | [`discord-bot`](docs/dev/discord-bot.md) |

### CWD (Current Working Directory)

The CWD is determined by where you launch `main.py`:
- File browser defaults to CWD (can navigate anywhere)
- Commits show the CWD's git repo
- Terminal starts in CWD
- Notes use the configurable notes dir (`merlin config notes-dir`)

### Session Management

> Full reference: [`docs/dev/session-management.md`](docs/dev/session-management.md)

Every conversation lives in a **Discord thread**, mapped 1:1 to a Claude Code session.

- **Channel message** → creates a thread, generates session via `uuid5("discord-thread-{thread_id}")`
- **Thread message** → looks up session from `data/session_registry.json`, resumes it
- **Reply to cron/bot message** → resumes the cron's session (tracked via `MERLIN_SESSION_ID` env var)

Strategy: **resume-first** — try `--resume` first, fall back to `--session-id` to create.

## Tech Stack

- **Language**: Python
- **Runner**: `uv run` (`pyproject.toml` + `uv.lock` for reproducible builds)
- **Web**: FastAPI + Jinja2 (server-side rendered)
- **LLM**: AgentEngine abstraction — Claude Code (default), OpenCode, extensible
- **Discord**: discord.py (listener) + httpx (REST API)
- **Scheduling**: Built-in asyncio scheduler (no cron dependency)

## Discord

> Full reference: [`docs/dev/discord-bot.md`](docs/dev/discord-bot.md)

- **Bot token**: `merlin-bot/.env` (see `.env.example`)
- **Default channel**: Set via `DISCORD_CHANNEL_IDS` in config
- **CLI**: `merlin chat --help`

## Cron Jobs

> Full reference: [`docs/dev/cron-system.md`](docs/dev/cron-system.md)

- **Core module**: `cron/` — scheduler, runner, state, REST API, logs, notifications
- **Job files**: `cron-jobs/*.json`
- **Management CLI**: `merlin cron --help`
- **REST API**: `/api/cron/jobs/*` — full CRUD + toggle + trigger + logs
- **Dashboard**: `/cron` — web UI for managing jobs
- **Scheduler**: Started from `main.py` via `cron.start()` (always runs, independent of merlin-bot)
- **Notifications**: Graceful fallback — Discord via merlin-bot if loaded, otherwise silent

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

# Full validation: lint + format + typecheck + tests
uv run scripts.py validate

# Lint only (no tests)
uv run scripts.py lint

# Unit + integration tests (~4s)
uv run scripts.py test

# E2E tests with Playwright (~2min)
uv run scripts.py test-e2e
```

## Logging

> Full reference: [`docs/dev/logging-system.md`](docs/dev/logging-system.md)

Three log types under `~/.merlin/`:

- **`logs/merlin.log`** — unified app log (`RotatingFileHandler`, 10 MB × 5). All modules use the `merlin.*` logger hierarchy.
- **`logs/engine-log.jsonl`** — engine lifecycle events (invocations, bot events, cron dispatches, app start/stop). Source of truth for the monitoring dashboard. Includes `stderr`, `request_id` for correlation.
- **`logs/raw-sessions/`** — raw engine output per invocation (stream-json). Powers the session viewer.

Rotation: `merlin.log` rotates by size, `engine-log.jsonl` keeps 180 days, `raw-sessions/` keeps 90 days. Cleanup runs at startup.

Extension loggers: use `from merlin_ext import get_logger` — see [`docs/dev/extension-system.md`](docs/dev/extension-system.md#logging).

## Monitoring Dashboard

> Full reference: [`docs/dev/dashboard-architecture.md`](docs/dev/dashboard-architecture.md) | Auth & tunnel: [`docs/dev/auth-and-tunnel.md`](docs/dev/auth-and-tunnel.md)

Web-based dashboard served by FastAPI on port 3123, started by `main.py`.

- **Auth:** Cookie-based auth (`DASHBOARD_USER` / `DASHBOARD_PASS` in `.env`) — see [`docs/dev/auth-and-tunnel.md`](docs/dev/auth-and-tunnel.md)
- **Core pages:** Files, Terminal, Commits (always available)
- **Built-in extensions:** Notes (enabled by default), Bot with tabs at `/bot` (disabled by default, requires Discord token)
- **Management pages:** Extensions (`/extensions`), Settings (gear dropdown → Settings)
- **Start:** `uv run main.py` starts everything (dashboard + bot + cron) in one process
- **Screenshots:** `uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --user admin --pass <pass>`

## Project Management

Epics and project planning are managed in the private `merlin-saas` repo under `epics/cli/`.

## Key Patterns and Conventions

- **pyproject.toml + uv.lock**: All dependencies declared in `pyproject.toml`, pinned by `uv.lock` for reproducible installs. Run `uv lock` after changing dependencies. Standalone Claude Code scripts (`.claude/skills/`) may still use PEP 723 inline metadata.
- **Self-documenting scripts**: Comprehensive `--help` with examples
- **Provider-agnostic execution**: Always use `lib/engine.py` (`invoke()`), never call `claude` or `opencode` directly. Engine configured via `AGENT_ENGINE` env var (default: `claude-code`). Available engines: `claude-code`, `opencode`. Merlin manages conversation history as JSONL files in `~/.merlin/sessions/`.
- **Deterministic sessions**: UUID5 from channel/job ID for session persistence
- **Extension system**: Three tiers — core (files, terminal, commits: always active), built-in (notes, merlin-bot: toggleable), installed (`~/.merlin/extensions/`: user-installed). Extensions export `router`, `NAV_ITEMS`, `STATIC_DIR`, plus optional `start()`, `on_tunnel_url()`, `validate()`. `main.py` builds an `extension_registry` at startup. State persisted in `~/.merlin/extensions.json`. Extensions page at `/extensions` for management.
- **Dynamic sidebar**: Nav items built from enabled extensions. Core items always shown, extension items added when loaded, Extensions nav item always last.
- **Path resolution (paths.py)**: All modules use `paths.py` for file/directory resolution. Only `app_dir()` differs between modes (repo root vs `~/.merlin/current/`). User data (notes, cron-jobs, logs, config) always lives under `~/.merlin/` regardless of mode. Dev mode detection: explicit `set_dev_mode()` > `MERLIN_DEV` env var > `.git/` directory presence. Custom install location via `MERLIN_HOME` env var.
- **Graceful degradation**: At startup, `_check_optional_deps()` checks for tmux and cloudflared. Missing deps result in boot warnings, disabled nav items (grayed out with tooltip), and 503 responses on affected routes — not crashes.
- **Fail-fast configuration**: All entry points (`merlin_bot.py`, `cron/runner.py`) validate required config at startup and exit immediately with descriptive error messages and step-by-step setup instructions if anything is missing or invalid. A first-time user should see exactly what to do — never a cryptic crash later at runtime. When adding new required config, always add validation to the entry point's `_validate_config()` function.
- **Web UI development**: Before making any dashboard or UI changes, read `docs/dev/dashboard-architecture.md` for theme variables, CSS conventions, JS patterns, API endpoints, and how to add new pages. Always self-validate UI changes by taking screenshots with the screenshot skill and reviewing them before marking work as done. Run `uv run .claude/skills/screenshot/screenshot.py --all <url> --user <user> --pass <pass>` from the project root, then read the PNGs to verify layout, responsiveness, and correctness across viewports.
