# OpenClaw Architecture Analysis

Reference analysis of the [OpenClaw](https://github.com/moltbot/moltbot) project to inform Merlin's design.

## Overview

OpenClaw is a ~442k LOC TypeScript monorepo (Node.js ≥22, pnpm workspaces). It is a self-hosted AI assistant supporting 30+ messaging channels, 60+ skills, and multiple LLM providers.

## Top-Level Structure

```
openclaw/
├── src/                 # Core TypeScript source
├── extensions/          # Plugin extensions (30+ channels/integrations)
├── skills/              # Custom skills/tools (60+ available)
├── packages/            # Compatibility shims (moltbot, clawdbot)
├── apps/                # Native applications (macOS, iOS, Android)
├── docs/                # Documentation
├── ui/                  # Web UI
├── test/                # Test fixtures and helpers
└── scripts/             # Build and deployment scripts
```

## Discord Integration

Located in `extensions/discord/`.

- Implements `ChannelPlugin<ResolvedDiscordAccount>` via the plugin SDK
- Capabilities: DMs, channels, threads, polls, reactions, media, native commands
- Message streaming: blocks streaming with `minChars: 1500, idleMs: 1000`
- Message chunking to handle Discord's size limits
- In-core Discord handling in `src/discord/`: sending, monitoring, user/channel resolution

**Merlin takeaway**: Reuse the message chunking concept for long LLM responses. Keep it simple with discord.py.

## Memory System

Located in `src/memory/` (38 files) and `extensions/memory-*`.

- **Storage**: SQLite with `sqlite-vec` vector extension
- **Search**: Hybrid approach combining BM25 full-text search + vector similarity
- **Embeddings**: Batch processing with deduplication, retry logic, concurrent workers
- **Providers**: OpenAI and Gemini embedding backends
- **Sessions**: Transcripts stored per agent under `~/.openclaw/agents/{agentId}/sessions/`
- **LanceDB extension**: Alternative vector backend with 19k LOC implementation

**Merlin takeaway**: Start with SQLite + keyword search. Vector search can be added later if needed.

## Cron / Scheduled Tasks

Located in `src/cron/`.

- Custom `CronService` class with start/stop/list/add/update/remove/run/wake methods
- File-based persistence for job definitions
- Jobs run in `"due"` (scheduled) or `"force"` (manual) modes
- Each job is associated with an agent that gets invoked
- Results stored in run logs with state/output
- Uses `croner` npm package for cron expression parsing
- Gateway integration triggers cron checks on heartbeat events

**Merlin takeaway**: Skip the custom cron service. Use Linux `crontab` directly with Python scripts that call the LLM and post results to Discord.

## LLM Calls

Located in `src/agents/models-config.providers.ts`.

Supported providers:
- Anthropic (Claude)
- OpenAI (GPT)
- Google Gemini
- AWS Bedrock
- GitHub Copilot
- Ollama (local)
- Qwen, Minimax, Xiaomi, Moonshot, Venice

Features:
- Round-robin rotation across multiple API keys
- Failure tracking and cooldown management
- Auth profiles with ordering by last-used timestamp
- Concurrent execution lanes per session
- History limiting per DM/session

**Merlin takeaway**: Only need Anthropic SDK. Single API key is fine to start.

## Plugin Architecture

- Plugin SDK in `src/plugin-sdk/` exports `ChannelPlugin`, `OpenClawPluginService`, `OpenClawPluginApi`
- Dynamic module loading via `jiti`
- Plugin registry and manifest system (`openclaw.plugin.json`)
- Channel plugins: discord, slack, telegram, signal, imessage, whatsapp, googlechat, matrix, mattermost, line, and more

**Merlin takeaway**: No plugin system needed. Discord-only, single-file scripts.

## Configuration

- Config file: `~/.openclaw/config.json` (JSON5 support)
- Zod schemas for validation
- CLI built with Commander.js (100+ commands)
- Environment variables via `.env`

**Merlin takeaway**: Use environment variables or a simple `.env` file for Discord token and API keys.

## Mapping to Merlin

| OpenClaw Component | Merlin Equivalent |
|---|---|
| `extensions/discord/` (plugin) | `bot.py` (discord.py) |
| `src/memory/` (SQLite + vectors) | SQLite or JSON file |
| `src/cron/` (custom service) | Linux crontab + scripts |
| Multi-provider LLM support | Anthropic SDK only |
| Plugin SDK + 30 channels | Discord only |
| 60+ skills | Custom tools as needed |
| TypeScript monorepo | Single Python scripts with `uv run` |
