# Merlin — Project

Merlin is a suite of dev tools built around the user's own AI agent CLI (Claude Code or Codex, run as they are): a web dashboard (terminal, files, commits), and a job scheduler whose every run opens a tmux window, running as one process, accessible from anywhere. Install via `curl | bash` or run from a git checkout with `uv run main.py`.

The web terminal is where the agent works; jobs are the runs that happen without you, and Merlin's own notifications tell you how they ended (see [`docs/dev/architecture.md`](docs/dev/architecture.md)). Merlin ships no tunnel of its own: remote access is bring-your-own tunnel/reverse proxy or Merlin Cloud (SaaS mode, `saas_tunnel.py`).

## Project Structure

```
merlin/
├── CLAUDE.md                  # This file — development instructions
├── main.py                    # FastAPI app — dashboard + jobs
├── cli.py                     # CLI entry point (merlin start/version/setup/update/config)
├── paths.py                   # Path resolution (dev mode vs installed mode)
├── install.sh                 # curl|bash installer
├── auth.py                    # Cookie-based auth (HMAC-signed)
├── saas_tunnel.py             # Merlin Cloud SSH tunnel (SaaS mode)
├── static/
│   ├── dashboard.css          # Design system (dark theme, CSS variables)
│   └── dashboard.js           # Shared JS (API, refresh, formatting)
├── templates/
│   ├── base.html              # Layout shell (dynamic sidebar)
│   └── login.html             # Auth page
├── lib/                       # Shared libraries (skills registry, event log writer and reader, hook files)
├── job/                       # Job core module (always active)
│   ├── __init__.py            # Scheduler loop (start(), _job_scheduler)
│   ├── runner.py              # Job dispatcher (check due, launch a run in its window, record it)
│   ├── state.py               # State/history/lock helpers
│   ├── manage.py              # Job CRUD + CLI for management
│   ├── routes.py              # REST API endpoints + /jobs dashboard page
│   ├── schemas.py             # Pydantic models (JobCreate, JobUpdate)
│   ├── logs.py                # Hybrid log storage (individual files + metadata)
│   ├── webhook.py             # Webhook trigger (single-flight launch, secret lifecycle)
│   └── templates/jobs.html    # Dashboard page template
├── webhooks/                  # Public webhook front desk (secret check, dispatch)
├── files/                     # File browser module
├── terminal/                  # Web terminal module (xterm.js + tmux)
├── commits/                   # Git commit browser module
├── board/                     # Sessions board — /api/board + assets for the terminal's Sessions drawer
├── workspace/                 # Workspace restore: continuous tmux snapshot, restore banner after a restart
├── voice/                     # Voice mode: hands-free voice conversation with a window's Claude Code agent (TTS client, transcript reader, sessions, console)
├── notes/                     # Notes editor module (markdown)
│   └── commands/              # merlin notes search / kb / remember commands
├── timeline/                  # Built-in private agent-activity Timeline extension
│   └── commands/emit.py       # Provider-neutral, server-independent emitter
├── skills/                    # Core operational skills (merlin/, merlin-jobs/, merlin-notes/, merlin-dashboard/, merlin-review/) — always active
├── tools/                     # Standalone scripts (kokoro-tts-server.py: the reference voice-mode TTS server)
├── tests/                     # Tests for core modules
├── agent/
│   └── MERLIN.md              # Agent brain doc (printed by `merlin agent`)
├── (epics managed in merlin-saas repo)
├── docs/                      # User/platform docs (for people using or extending Merlin)
│   └── dev/                   # Contributor docs (for changing Merlin's code; see list below)
```

### Reference Documentation

New to the codebase? Read `architecture.md` first, then `extension-system.md` and `skill-system.md` to see how everything plugs in. The rest are per-system references; read them when working on the corresponding system.

| Doc | Covers |
|-----|--------|
| [`docs/dev/development-setup.md`](docs/dev/development-setup.md) | Contributor setup: run from a checkout, validate/test commands, the throwaway instance |
| [`docs/dev/architecture.md`](docs/dev/architecture.md) | High-level system overview, data flow |
| [`docs/dev/job-system.md`](docs/dev/job-system.md) | Job format, dispatcher, state/locks, scheduler, staleness guard |
| [`docs/dev/notes-system.md`](docs/dev/notes-system.md) | 3-layer notes system (user, logs, KB), frontmatter format, search tools |
| [`docs/dev/auth-and-tunnel.md`](docs/dev/auth-and-tunnel.md) | Cookie auth, HMAC signing, login flow |
| [`docs/dev/web-terminal.md`](docs/dev/web-terminal.md) | Web terminal internals: xterm.js, WebSocket, PTY/tmux, touch gesture implementation, transcription API |
| [`docs/dev/notes-editor.md`](docs/dev/notes-editor.md) | Notes routes, command palette, git ops, media upload, content search |
| [`docs/dev/agent-activity-timeline.md`](docs/dev/agent-activity-timeline.md) | Timeline schema, consent, hooks, emitter, store, query API, and live UI |
| [`docs/dev/extension-system.md`](docs/dev/extension-system.md) | Extension tiers, interface, feature flags, state, registry, Extensions/Settings pages |
| [`docs/dev/app-streaming.md`](docs/dev/app-streaming.md) | Flagged Apps built-in: Xvfb sessions, `merlin app` CLI, WebRTC streamer, terminal panel, player, Apps page |
| [`docs/dev/skill-system.md`](docs/dev/skill-system.md) | Skill registry: sources, precedence (core > extension > user), canonical aggregation, engine adapters, shims, `merlin skills` |
| [`docs/dev/dashboard-architecture.md`](docs/dev/dashboard-architecture.md) | Dashboard theme, CSS variables, JS patterns, API endpoints |
| [`docs/dev/commit-review.md`](docs/dev/commit-review.md) | Commits page comparisons (`base..head`, ref safety, routes, the sticky header), saved reviews, comments, `merlin review` |
| [`docs/dev/notifications.md`](docs/dev/notifications.md) | Attention watcher, event cursor, poll transport, in-tab rule, manifest and worker, Web Push, suppression, the seams kept for the hub |
| [`docs/dev/voice-mode.md`](docs/dev/voice-mode.md) | Voice mode: the OpenAI-compatible TTS seam, config keys, the voice context hook and tmux options, the transcript reader (Codex seam), sessions, control frames, the console, the reference Kokoro server |
| [`docs/dev/workspace-restore.md`](docs/dev/workspace-restore.md) | Continuous tmux snapshot, the startup freeze, the restore banner and engine, `@agent_conv` hook stamping |
| [`docs/dev/notifications-acceptance.md`](docs/dev/notifications-acceptance.md) | What only a person can verify on notifications: real push delivery, real focus, real agent screens, real browsers, the phone as an app |
| [`docs/dev/claude-code-reference.md`](docs/dev/claude-code-reference.md) | Claude Code CLI flags and options |
| [`docs/dev/releasing.md`](docs/dev/releasing.md) | Tagging, GitHub Releases, install/update flow, rollback |
| [`docs/dev/standalone-cli.md`](docs/dev/standalone-cli.md) | Standalone CLI design: paths, install, update, dev mode |
| [`docs/dev/logging-system.md`](docs/dev/logging-system.md) | Logging architecture, log files, rotation, engine log events |
| [`docs/dev/openclaw-architecture.md`](docs/dev/openclaw-architecture.md) | Design-reference analysis of OpenClaw (not Merlin internals) |

### Doc Audiences

1. **`merlin/CLAUDE.md`** (this file) — For **developing** Merlin. No operational agent guidance lives here.
2. **`agent/MERLIN.md`** — The agent brain doc: what Merlin is and how to operate it, channel-neutral. Printed by `merlin agent`.

## Script Documentation Convention

**All scripts are self-documented via `--help`.** To understand any script:

```bash
uv run main.py --help              # Core entry point
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
│      (FastAPI + auth + extensions — one process)          │
│                                                           │
│  Core Modules:                                            │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────┐   │
│  │  files/   │ │terminal/ │ │ commits/ │ │  job/      │   │
│  │  Browser  │ │ xterm.js │ │ Git log  │ │ Scheduler  │   │
│  └──────────┘ └──────────┘ └──────────┘ └───────────┘   │
│                                                           │
│  Built-in Extensions:                                     │
│  ┌───────────┐ ┌───────────┐                             │
│  │  notes/    │ │ timeline/ │                             │
│  │  Markdown  │ │ (flagged) │                             │
│  └───────────┘ └───────────┘                             │
│                                                           │
│  Installed Extensions: ~/.merlin/extensions/               │
└──────────────────────────────────────────────────────────┘
                          │
                   ┌──────┴──────┐
                   │             │
                   ▼             ▼
                Merlin        Browser
                Cloud        (mobile/
              (SaaS mode)     desktop)
```

### Components

**Core (project root):**

| File | Purpose | Docs |
|------|---------|------|
| `main.py` | FastAPI app — starts dashboard + jobs (one process) | `--help` |
| `cli.py` | CLI entry point — `merlin start/version/setup/update/config` | `--help`, [`standalone-cli`](docs/dev/standalone-cli.md) |
| `paths.py` | Path resolution — dev mode vs installed mode (`~/.merlin/`) | [`standalone-cli`](docs/dev/standalone-cli.md) |
| `install.sh` | `curl \| bash` installer | [`releasing`](docs/dev/releasing.md) |
| `auth.py` | Cookie-based HMAC auth | [`auth-and-tunnel`](docs/dev/auth-and-tunnel.md) |
| `lib/skills.py` | Skill registry, canonical aggregation, shims, the agent-state hook reconcilers | [`skill-system`](docs/dev/skill-system.md) |
| `job/` | Job core module — scheduler, runner, runs in tmux windows, supervisor, retention, REST API, logs | [`job-system`](docs/dev/job-system.md) |
| `files/` | File browser module | [`dashboard-architecture`](docs/dev/dashboard-architecture.md) |
| `terminal/` | Web terminal module | [`web-terminal`](docs/dev/web-terminal.md) |
| `commits/` | Commit browser module | [`dashboard-architecture`](docs/dev/dashboard-architecture.md) |
| `notes/` | Notes editor module | [`notes-editor`](docs/dev/notes-editor.md) |
| `voice/` | Voice mode core module (`/api/voice`), plus `tools/kokoro-tts-server.py` | [`voice-mode`](docs/dev/voice-mode.md) |
| `timeline/` | Built-in private activity-history extension | [`agent-activity-timeline`](docs/dev/agent-activity-timeline.md) |
| `app/` | Built-in app streaming (GUI apps on Xvfb, `merlin app`, WebRTC), only with `MERLIN_FEATURES=app` | [`app-streaming`](docs/dev/app-streaming.md) |
| `features.py` | Feature flags (`MERLIN_FEATURES`) for experimental built-ins | [`extension-system`](docs/dev/extension-system.md#feature-flags) |

### CWD (Current Working Directory)

The CWD is determined by where you launch `main.py`:
- File browser defaults to CWD (can navigate anywhere)
- Commits show the CWD's git repo
- Terminal starts in CWD
- Notes use the configurable notes dir (`merlin config notes-dir`)

## Tech Stack

- **Language**: Python
- **Runner**: `uv run` (`pyproject.toml` + `uv.lock` for reproducible builds)
- **Web**: FastAPI + Jinja2 (server-side rendered)
- **Agents**: the user's own Claude Code or Codex, run as they are in tmux windows (no wrapper, no engine)
- **Scheduling**: Built-in asyncio scheduler (no cron dependency)

## Jobs

> Full reference: [`docs/dev/job-system.md`](docs/dev/job-system.md)

- **Core module**: `job/` — scheduler, runner, state, REST API, logs, notifications
- **Triggers**: optional `schedule` (cron) and/or `webhook` (secret-gated HTTP via the `webhooks/` front desk); neither = manual-only
- **Job files**: `jobs/*.json`
- **Management CLI**: `merlin job --help`
- **REST API**: `/api/jobs/jobs/*` — full CRUD + toggle + trigger + logs
- **Dashboard**: `/jobs` — web UI for managing jobs
- **Scheduler**: Started from `main.py` via `job.start()` (always runs)
- **Notifications**: through Merlin's own attention watcher, by the job's `report_mode` (see [`docs/dev/notifications.md`](docs/dev/notifications.md))

## Environment

Reference dev environment (the maintainer's setup, not a requirement). Merlin itself needs Python + uv, optionally tmux.

- **OS**: Arch Linux (Docker)
- **Package Manager**: pacman
- **Available Ports**: 3123, 3124, 3125

## Development Commands

```bash
uv run main.py                # start everything (dashboard + bot + jobs, dev mode)
uv run cli.py restart         # restart the running server (stop + fresh start)
uv run cli.py stop            # stop it without restarting
uv run scripts.py validate    # full validation: lint + format + typecheck + tests
```

Restart/stop live in `server_control.py` (behind `merlin restart` / `merlin stop`
and the `/api/restart` + `/api/update` endpoints). The running server publishes
`~/.merlin/data/server-state.json` (`{pid, port}`, written atomically after the
socket binds); restart reads it to stop that exact PID (validated with `ps`, so a
lookalike is never killed) and to relaunch on the same port. Readers ignore stale
state whose PID is not a live Merlin; a user-scoped pattern-kill is the fallback
only when there is no valid state (it also reads the legacy `server-pid` file so a
restart can stop a server started by the previous release). Restart cannot happen
in-process — the endpoints spawn a detached `merlin restart`, which relaunches via
a fresh `uv run` (clean venv after an update's symlink flip). The bind port
resolves as `--port` > `MERLIN_DASHBOARD_PORT` (config) > 3123. Set
`MERLIN_SUPERVISED=1` (in the
systemd/launchd unit, see `deploy/`) when a service manager owns the lifecycle:
restart/update then stop-and-exit instead of self-relaunching. See
[`docs/getting-started.md`](docs/getting-started.md) → "Run Merlin as a service".
`restart.sh` is a deprecated one-release shim that forwards to `merlin restart`.

Full setup, test variants, bot-from-checkout, and E2E first-time setup:
[`docs/dev/development-setup.md`](docs/dev/development-setup.md).

## Logging

> Full reference: [`docs/dev/logging-system.md`](docs/dev/logging-system.md)

Two log types under `~/.merlin/`:

- **`logs/merlin.log`** — unified app log (`RotatingFileHandler`, 10 MB × 5). All modules use the `merlin.*` logger hierarchy.
- **`logs/engine-log.jsonl`** — lifecycle events (app start/stop, job runner crashes, webhook requests, routed notifications). The jobs page reads the crashes and the webhook requests from it.

Rotation: `merlin.log` rotates by size, `engine-log.jsonl` keeps 180 days. Cleanup runs at startup; a `logs/raw-sessions/` left by an older release is never touched.

Extension loggers: use `from merlin_ext import get_logger` — see [`docs/dev/extension-system.md`](docs/dev/extension-system.md#logging).

## Monitoring Dashboard

> Full reference: [`docs/dev/dashboard-architecture.md`](docs/dev/dashboard-architecture.md) | Auth: [`docs/dev/auth-and-tunnel.md`](docs/dev/auth-and-tunnel.md)

Web-based dashboard served by FastAPI on port 3123, started by `main.py`.

- **Auth:** Cookie-based auth (`DASHBOARD_USER` / `DASHBOARD_PASS` in `.env`) — see [`docs/dev/auth-and-tunnel.md`](docs/dev/auth-and-tunnel.md)
- **Core pages:** Files, Terminal, Commits (always available)
- **Built-in extensions:** Notes and Timeline
- **Management pages:** Extensions (`/extensions`), Settings (gear dropdown → Settings)
- **Start:** `uv run main.py` starts everything (dashboard + jobs) in one process
- **Screenshots:** `uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --user admin --pass <pass>`

## Project Management

Epics and project planning are managed in the private `merlin-saas` repo under `epics/cli/`.

## Key Patterns and Conventions

- **pyproject.toml + uv.lock**: All dependencies declared in `pyproject.toml`, pinned by `uv.lock` for reproducible installs. Run `uv lock` after changing dependencies. Standalone Claude Code scripts (`.claude/skills/`) may still use PEP 723 inline metadata.
- **Self-documenting scripts**: Comprehensive `--help` with examples
- **No agent wrapper**: a job runs the agent's real interface (`claude`, `codex`) in a tmux window (`job/agents.py`, `job/runs.py`); Merlin never drives an agent through a subprocess API or a session file of its own.
- **Extension system**: Three tiers — core (files, terminal, commits: always active), built-in (notes, timeline: toggleable), installed (`~/.merlin/extensions/`: user-installed). Extensions export `api_router`/`page_router` (mounted at `/api/{slug}` and `/{slug}`, slug from optional `URL_SLUG`), `NAV_ITEMS`, `STATIC_DIR`, plus optional `register_routes(app)` escape hatch, `start()`, `disable()`, `validate()`. `disable()` performs bounded cleanup before an enabled extension is toggled off; failure leaves it enabled. `main.py` builds an `extension_registry` at startup. State persisted in `~/.merlin/extensions.json`. Extensions page at `/extensions` for management.
- **Dynamic sidebar**: Nav items built from enabled extensions. Core items always shown, extension items added when loaded, Extensions nav item always last.
- **Path resolution (paths.py)**: All modules use `paths.py` for file/directory resolution. Only `app_dir()` differs between modes (repo root vs `~/.merlin/current/`). User data (notes, jobs, logs, config) always lives under `~/.merlin/` regardless of mode. Dev mode detection: explicit `set_dev_mode()` > `MERLIN_DEV` env var > `.git/` directory presence. Custom install location via `MERLIN_HOME` env var.
- **Graceful degradation**: At startup, `_check_optional_deps()` checks for tmux. Missing deps result in boot warnings, disabled nav items (grayed out with tooltip), and 503 responses on affected routes — not crashes.
- **Fail-fast configuration**: All entry points (`main.py`, `job/runner.py`) validate required config at startup and exit immediately with descriptive error messages and step-by-step setup instructions if anything is missing or invalid. A first-time user should see exactly what to do — never a cryptic crash later at runtime. When adding new required config, always add validation to the entry point's `_validate_config()` function.
- **Web UI development**: Before making any dashboard or UI changes, read `docs/dev/dashboard-architecture.md` for theme variables, CSS conventions, JS patterns, API endpoints, and how to add new pages. Always self-validate UI changes by taking screenshots with the screenshot skill and reviewing them before marking work as done. Run `uv run .claude/skills/screenshot/screenshot.py --all <url> --user <user> --pass <pass>` from the project root, then read the PNGs to verify layout, responsiveness, and correctness across viewports. When the shot needs UI state you do not currently have (agent-state pills in every state, a populated Sessions panel), build a disposable instance rather than fabricating that state in your own environment: `docs/dev/development-setup.md` ("Throwaway instance for UI work") carries the recipe and, more importantly, the two teardown traps that have each destroyed a live working session.
