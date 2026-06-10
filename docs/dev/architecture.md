# Merlin — System Architecture

Merlin is a personal AI assistant with a web dashboard. It uses an **AgentEngine** abstraction to invoke any AI coding tool (Claude Code, OpenCode, etc.) and manages its own conversation history as JSONL transcripts. The bot and cron handlers capture engine output and deliver it to the appropriate channel (Discord, etc.).

## System at a glance

Four planes, one process:

- **Dashboard surface**: files, terminal, commits, notes pages served by FastAPI (see [`dashboard-architecture.md`](dashboard-architecture.md), [`web-terminal.md`](../web-terminal.md))
- **Agent loop**: chat and cron invocations through `lib/engine.py`, with persona composition in `lib/agent_context.py` (this doc, below)
- **Scheduling**: the cron core module dispatching agent jobs (see [`cron-system.md`](cron-system.md))
- **Extensibility**: extensions contribute commands, skills, and pages (see [`extension-system.md`](extension-system.md), [`skill-system.md`](skill-system.md))

## Entry Points

```
╔═══════════════════════════════════════════════════════════════════════════╗
║                           ENTRY POINTS                                  ║
╠═══════════════════════════╦═══════════════════════════════════════════════╣
║                           ║                                             ║
║   USER (Discord)          ║   TIME (cron/ scheduler in main.py)         ║
║   Sends a message         ║   Fires every minute                        ║
║                           ║                                             ║
╚═════════════╤═════════════╩═══════════════════╤═══════════════════════════╝
              │                                 │
              ▼                                 │
┌──────────────────────────┐                    │
│      Discord API         │                    │
│   (discord.com gateway)  │                    │
│                          │                    │
│  Pushes events via       │                    │
│  WebSocket to bot        │                    │
└────────────┬─────────────┘                    │
             │                                  │
             ▼                                  ▼
┌──────────────────────────┐     ┌──────────────────────────────────┐
│     merlin_bot.py        │     │        cron/runner.py             │
│   (Discord handler)      │     │     (spawned subprocess)          │
│                          │     │                                   │
│  on_message():           │     │  1. Load cron-jobs/*.json         │
│  1. Filter: bot? allowed │     │  2. croniter: is job due?         │
│     channel?             │     │  3. Execute via lib/engine.py     │
│  2. Thread or channel?   │     │  4. Log + emit job_complete JSON  │
│  3. Build rich prompt    │     │                                   │
│  4. Add thinking reaction│     └───────────────┬───────────────────┘
│                          │                     │
└────────────┬─────────────┘                     │
             │                                   │
             │  invoke(prompt,                   │  invoke(prompt,
             │    session_id=uuid5(thread))       │    session_id=uuid4() or uuid5(job_id))
             │                                   │
             ▼                                   ▼
┌══════════════════════════════════════════════════════════════════════════┐
║                        lib/engine.py                                    ║
║             (Provider-agnostic entry point — ALL calls go here)         ║
║                                                                         ║
║  1. Assemble system prompt from caller-provided parts                    ║
║     (callers compose brain/personality/user via lib/agent_context.py)    ║
║  2. Load session history (~/.merlin/sessions/<session_id>.jsonl)         ║
║  3. Get configured engine (AGENT_ENGINE env var, default: claude-code)   ║
║  4. engine.invoke(prompt, history, system_prompt)                        ║
║  5. Record turns to session JSONL                                        ║
║  6. Save raw session → logs/raw-sessions/<timestamp>.jsonl               ║
║  7. Log structured event → logs/engine-log.jsonl                         ║
║  8. Return AgentResult                                                   ║
║                                                                         ║
╚════════════════════════════════╤═════════════════════════════════════════╝
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
        ▼                        ▼                        ▼
┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ ClaudeCode   │   │   OpenCode       │   │   (future        │
│ Engine       │   │   Engine         │   │    engines)      │
│              │   │                  │   │                  │
│ claude -p    │   │ opencode run     │   │                  │
└──────┬───────┘   └────────┬─────────┘   └──────────────────┘
       │                    │
       │  subprocess.run    │  subprocess.run
       ▼                    ▼
   AI coding tool returns text output (stdout)
                                 │
                                 │  AgentResult.content
                                 ▼
             ┌───────────────────────────────────────┐
             │         Caller handles delivery        │
             │                                       │
             │  Bot: send_message(thread_id, content) │
             │  Cron: notify.py → Discord channel     │
             └───────────────────────────────────────┘
```

## Session Continuity

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Session Management                                │
│                                                                     │
│  Merlin manages its own conversation history as JSONL files.        │
│  No reliance on any engine's built-in session/resume mechanism.     │
│                                                                     │
│  Location: ~/.merlin/sessions/<session_id>.jsonl                    │
│                                                                     │
│  Format (one JSON line per entry):                                  │
│    Header: {"v":1, "session_id":"...", "engine":"...", "model":"..."}│
│    Turn:   {"role":"user", "content":"...", "ts":"..."}             │
│    Turn:   {"role":"assistant", "content":"...", "duration":3.2}    │
│                                                                     │
│  Session resolution (unchanged):                                    │
│    Channel msg  → create thread → uuid5("discord-thread-{id}")      │
│    Thread msg   → lookup registry → use existing session            │
│    Cron job     → uuid4() per run (ephemeral default);              │
│                   uuid5("cron-job-{job_id}") if "ephemeral": false  │
│    Reply to bot → lookup message_id → resume that session           │
│                                                                     │
│  Compaction: when history exceeds engine context window,            │
│  oldest turns are dropped (system prompt + recent turns kept).      │
│                                                                     │
│  Registry: session_registry.py (thread/message → session mapping)   │
│  Persists across restarts (JSON on disk, file-locked)               │
└─────────────────────────────────────────────────────────────────────┘
```

## Memory System

```
notes/
├── user.md           ← User memory layer (injected by the managed recipes)
├── logs/
│   └── YYYY-MM-DD.md ← Daily notes, decisions, discoveries
└── kb/               ← Zettelkasten knowledge base
    ├── _index.md       (entry point)
    └── *.md            (atomic, interlinked notes)
```

Both loops, and the user in the web terminal, read and write this tree through the notes CLI (`merlin notes`, `merlin kb`, `merlin remember`). Cron jobs write findings into `kb/` and `logs/`; the bot reads `user.md` and the KB through its agent_context recipe. One assistant, several entry points, one accumulating memory: the shared state is what makes invocations compound instead of starting cold.

## The Three Loops

| Loop | Trigger | Path |
|------|---------|------|
| **Discord** | User sends message | Discord Gateway → `merlin_bot.py` → `lib/engine.py` → engine → `AgentResult` → bot sends to Discord |
| **Cron** | `cron/` scheduler (every min) | `main.py` → `cron/runner.py` (subprocess) → `lib/engine.py` → engine → `AgentResult` → `notify.py` sends to Discord |
| **Terminal** | You, in the web terminal | interactive agent CLIs (Claude Code, etc.) get the same skills via the shims and the same notes/KB; `merlin agent` prints the brain doc on demand |

The Discord and cron loops converge at `lib/engine.py` — the single chokepoint where every managed invocation is logged and sessions are managed (the terminal loop runs the agent CLIs directly). The engine is a black box — it has no notion of Discord, delivery, or persona. Contextual system-prompt content (brain doc, personality, user memory, channel overlays) is composed by `lib/agent_context.py`: each managed caller selects a recipe (the bot: managed-assistant; cron agent jobs: headless-worker) and passes the result into `invoke()`.

The cron system is a **core module** started from `main.py`, independent of merlin-bot. The bot extension only provides Discord connectivity; cron works standalone (notifications are silently skipped if the bot is not loaded).

## Agent context composition (`lib/agent_context.py`)

Contextual system-prompt content is composed above the engine, in `lib/agent_context.py`. Layers, each defined once there (legacy fallback paths noted in the module):

| Layer | Source |
|-------|--------|
| brain | `agent/MERLIN.md` (app dir, refreshed by `merlin update`) |
| personality | `~/.merlin/personality.md` |
| user memory | `~/.merlin/user.md` (fallback: `<notes-dir>/user.md`) |
| channel overlay | e.g. `merlin-bot/discord_directives.md` |

Recipes select layer sets per caller:

| Recipe | Caller | Layers |
|--------|--------|--------|
| managed-assistant | Discord bot | brain + personality + user + Discord overlay |
| headless-worker | cron agent jobs | brain + user (no personality by design) |
| interactive | `merlin agent` | brain printed; `--personality` / `--user` append the same layers |

The composed prompt reaches the engine through `invoke(append_system_prompt=...)`; the engine stays channel-agnostic and appends its own skill fallback (non-native engines) after it, so the brain always precedes the skill table. Skills are not a layer; they are always-on and owned by the engine adapters (see [`skill-system.md`](skill-system.md)).

## Path Resolution

```
┌─────────────────────────────────────────────────────────────────────┐
│                         paths.py                                     │
│              (All modules import paths for file resolution)          │
│                                                                     │
│  Dev mode detection:                                                │
│    1. Explicit set_dev_mode() call (--dev flag)                     │
│    2. MERLIN_DEV=1 env var                                          │
│    3. .git/ directory in paths.py parent                            │
│                                                                     │
│  Dev mode (git checkout):        Installed mode (~/.merlin/):       │
│  ┌─────────────────────────┐    ┌──────────────────────────────┐   │
│  │ app_dir → repo root     │    │ app_dir → ~/.merlin/current/ │   │
│  │ data_dir → ~/.merlin/   │    │ data_dir → ~/.merlin/        │   │
│  │ config → .env           │    │ config → config.env          │   │
│  │ notes → ~/.merlin/notes/ │    │ notes → ~/.merlin/notes/     │
│  │                         │    │                              │   │
│  └─────────────────────────┘    └──────────────────────────────┘   │
│                                                                     │
│  MERLIN_HOME overrides ~/.merlin/, NOTES_DIR overrides notes path   │
└─────────────────────────────────────────────────────────────────────┘
```

## Startup Flow

```
cli.py (merlin start)
  │
  ├── set_dev_mode() if --dev
  ├── Check for config.env → run_setup() if missing
  │
  └── main.start_server(port, host, no_tunnel)
        │
        ├── _validate_config()
        │     ├── Check DASHBOARD_PASS (auto-generate if tunnel enabled)
        │     └── _check_optional_deps()
        │           ├── tmux missing → TMUX_AVAILABLE=False, disable nav item
        │           └── cloudflared missing → TUNNEL_ENABLED=False
        │
        ├── _rebuild_skill_registry()  (aggregate ~/.merlin/skills, refresh
        │     the ~/.claude & ~/.agents shims — see skill-system.md)
        │
        └── asyncio.run()
              ├── uvicorn.Server (FastAPI app)
              ├── start_tunnel() (if enabled)
              └── extension.start() for each extension with start() hook
```

## Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, startup, extension loader, settings API |
| `cli.py` | CLI entry point and subcommands (`start`, `version`, `setup`, `update`, `config`, `skills`, `agent`, `cron`, `chat`, `dashboard-url`) |
| `paths.py` | Path resolution (dev mode vs installed) |
| `lib/engine.py` | AgentEngine abstraction, `invoke()` entry point, engine registry |
| `lib/session.py` | JSONL session manager (create, load, append, compact) |
| `lib/skills.py` | Skill registry, canonical aggregation, engine adapters, shims (see [`skill-system.md`](skill-system.md)) |
| `lib/engines/claude_code.py` | Claude Code CLI engine (default) |
| `lib/engines/opencode.py` | OpenCode CLI engine |
| `merlin-bot/merlin_bot.py` | Discord handler, session resolution, prompt building |
| `merlin-bot/discord_send.py` | Discord REST transport (send/reply/react/rename; CLI: `merlin chat`) |
| `merlin-bot/session_registry.py` | Thread/message → session mapping |
| `cron/runner.py` | Job dispatcher (check due, execute via engine) |
| `cron/notify.py` | Notification delivery (report_mode logic, Discord fallback) |
| `cron/__init__.py` | Scheduler loop |
