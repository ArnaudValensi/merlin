# Merlin — System Architecture

## Entry Points

```
╔═══════════════════════════════════════════════════════════════════════════╗
║                           ENTRY POINTS                                  ║
╠═══════════════════════════╦═══════════════════════════════════════════════╣
║                           ║                                             ║
║   USER (Discord)          ║   TIME (merlin_bot.py scheduler)             ║
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
│     merlin_bot.py        │────▶│        cron_runner.py            │
│    (discord.py client)   │     │      (spawned subprocess)        │
│                          │     │                                  │
│  on_message():           │     │  1. Load cron-jobs/*.json        │
│  1. Filter: bot? allowed │     │  2. croniter: is job due?        │
│     channel?             │                    │
│  2. Thread or channel?   │                    │
│     ┌────────────────┐   │                    │
│     │ Channel msg:   │   │                    │
│     │ → create thread│   │                    │
│     │ Thread msg:    │   │                    │
│     │ → lookup sesion│   │                    │
│     └────────────────┘   │                    │
│  3. Build rich prompt:   │                    │
│     [Discord msg from    │                    │
│      "user" in thread X, │                    │
│      channel Y, msg Z]   │                    │
│  4. Add thinking reaction│                    │
│                          │                    │
└────────────┬─────────────┘                    │
             │                                  │
             │  invoke_claude(prompt,            │  invoke_claude(prompt,
             │    session=uuid5(thread),         │    session=uuid5(job_id),
             │    resume=True)                   │    resume=True)
             │                                  │
             ▼                                  ▼
┌══════════════════════════════════════════════════════════════════════════┐
║                      claude_wrapper.py                                  ║
║                  (Single entry point — ALL calls go here)               ║
║                                                                         ║
║  1. Load user memory (memory/user.md) → append to system prompt         ║
║  2. Build CLI command:                                                  ║
║     claude -p --output-format json                                      ║
║       --dangerously-skip-permissions                                    ║
║       --resume <session_id>  (or --session-id if new)                   ║
║       --append-system-prompt <user_memory>                              ║
║       "<prompt>"                                                        ║
║  3. Set MERLIN_SESSION_ID env var for child processes                   ║
║  4. subprocess.run() → capture stdout/stderr                            ║
║  5. Parse JSON output (result, session_id, usage)                       ║
║  6. Write invocation log → logs/claude/<timestamp>-<caller>.log         ║
║  7. Return ClaudeResult                                                 ║
║                                                                         ║
║  Resume-first strategy:                                                 ║
║    try --resume → if "No conversation found" → retry --session-id       ║
║                                                                         ║
╚════════════════════════════════╤═════════════════════════════════════════╝
                                 │
                                 │  subprocess.run(["claude", "-p", ...])
                                 ▼
┌══════════════════════════════════════════════════════════════════════════┐
║                        Claude Code CLI                                  ║
║              (runs from merlin-bot/, reads CLAUDE.md)                   ║
║                                                                         ║
║  Loaded context:                                                        ║
║  ┌─────────────────────────────────────────────────────────────┐        ║
║  │  merlin-bot/CLAUDE.md  →  Personality, directives,          │        ║
║  │                            response format, memory system   │        ║
║  │  memory/user.md        →  User facts (via system prompt)    │        ║
║  └─────────────────────────────────────────────────────────────┘        ║
║                                                                         ║
║  Available tools:   Bash, Read, Write, Grep, WebSearch, etc.            ║
║                                                                         ║
║  Skills (via Bash):                                                     ║
║  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────────┐        ║
║  │  Discord Skill  │  │  Cron Skill     │  │  Memory Skill    │        ║
║  │  discord_send.py│  │  cron_manage.py │  │  memory_search.py│        ║
║  │  (send/reply/   │  │  (add/list/     │  │  kb_add.py       │        ║
║  │   react)        │  │   edit/toggle)  │  │  remember.py     │        ║
║  └────────┬────────┘  └────────────────┘  └──────────────────┘        ║
║           │                                                             ║
╚═══════════╪═════════════════════════════════════════════════════════════╝
            │
            │  uv run discord_send.py reply
            │    --channel <thread_id>
            │    --message <msg_id>
            │    --content "response"
            ▼
┌──────────────────────────────┐
│      discord_send.py         │
│                              │
│  1. Load bot token (.env)    │
│  2. Chunk msg if > 2000 ch   │
│  3. POST to Discord REST API │
│  4. Register message→session │◄── MERLIN_SESSION_ID env var
│     in session_registry.py   │    (for cron continuation)
│                              │
└──────────────┬───────────────┘
               │
               │  httpx POST
               ▼
┌──────────────────────────────┐
│      Discord REST API        │
│   discord.com/api/v10        │
│                              │
│  → Message appears in thread │
│  → User sees the response    │
└──────────────────────────────┘
```

## Session Continuity

```
┌─────────────────────────────────────────────────────────────────────┐
│                    session_registry.py                               │
│                data/session_registry.json                            │
│                                                                     │
│    threads: { thread_id → session_id }   (Discord conversations)    │
│    messages: { message_id → session_id } (cron message tracking)    │
│                                                                     │
│  How sessions are created:                                          │
│    Channel msg  → create thread → uuid5("discord-thread-{id}")      │
│    Thread msg   → lookup registry → resume existing session         │
│    Cron job     → uuid5("cron-job-{job_id}")  (deterministic)       │
│    Reply to bot → lookup message_id → resume that session           │
│                                                                     │
│  Written by: merlin_bot.py (threads), discord_send.py (messages)    │
│  Read by:    merlin_bot.py (session resolution)                     │
│  Persists across bot restarts (JSON on disk, file-locked)           │
└─────────────────────────────────────────────────────────────────────┘
```

## Memory System

```
memory/
├── user.md           ← Auto-loaded into every Claude call (via system prompt)
├── logs/
│   └── YYYY-MM-DD.md ← Daily notes, decisions, discoveries
└── kb/               ← Zettelkasten knowledge base
    ├── _index.md       (entry point)
    └── *.md            (atomic, interlinked notes)
```

## The Two Loops

| Loop | Trigger | Path |
|------|---------|------|
| **Discord** | User sends message | Discord Gateway → `merlin_bot.py` → `claude_wrapper.py` → `claude -p` → skills → `discord_send.py` → Discord API → User |
| **Cron** | `merlin_bot.py` scheduler (every min) | `merlin_bot.py` → `cron_runner.py` (subprocess) → `claude_wrapper.py` → `claude -p` → skills → `discord_send.py` → Discord API → Channel |

Both loops converge at `claude_wrapper.py` — the single chokepoint where every invocation is logged, sessions are managed, and user memory is injected.

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
│  │ data_dir → merlin-bot/  │    │ data_dir → ~/.merlin/        │   │
│  │ config → .env           │    │ config → config.env          │   │
│  │ memory → merlin-bot/    │    │ memory → ~/.merlin/memory/   │
│  │           memory/       │    │                              │   │
│  └─────────────────────────┘    └──────────────────────────────┘   │
│                                                                     │
│  MERLIN_HOME env var overrides ~/.merlin/ location                  │
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
        └── asyncio.run()
              ├── uvicorn.Server (FastAPI app)
              ├── start_tunnel() (if enabled)
              └── bot_plugin.start() (if loaded)
```
