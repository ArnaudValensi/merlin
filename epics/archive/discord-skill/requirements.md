# Discord Skill — Requirements

## Goal

Give the Claude Code instance (run via the wrapper) the ability to interact with Discord: send messages, reply to messages, and react to messages. This is the primary output channel for Merlin.

## Context

The wrapper (`claude_wrapper.py`) invokes `claude -p` as a subprocess. Claude Code needs a skill it can use to send responses back to Discord. The skill is a SKILL.md file that instructs Claude to call a Python script via Bash. The script uses the Discord REST API directly (no discord.py dependency for sending — just HTTP requests).

## Requirements

### R1: Wrapper runs from merlin-bot/ directory
- **Status**: `accepted`
- `invoke_claude()` must pass `cwd` to `subprocess.run()` so Claude Code always starts in `merlin-bot/`
- This ensures Claude Code picks up `merlin-bot/CLAUDE.md` and `merlin-bot/.claude/` (skills, settings)

### R2: Discord sending script
- **Status**: `accepted`
- Standalone Python script (`merlin-bot/discord_send.py`) run with `uv run`
- PEP 723 inline dependencies (e.g. `httpx` or `requests`)
- Loads bot token from `merlin-bot/.env` (`DISCORD_BOT_TOKEN`)
- Supports three actions:
  - **send**: Send a message to a channel. Handles chunking for Discord's 2000-char limit.
  - **reply**: Send a message as a reply to a specific message (uses `message_reference`). Also handles chunking.
  - **react**: Add a reaction emoji to a message.
- CLI interface so Claude can call it via Bash:
  ```
  uv run discord_send.py send --channel <id> --content "message"
  uv run discord_send.py reply --channel <id> --message <id> --content "message"
  uv run discord_send.py react --channel <id> --message <id> --emoji "✅"
  ```
- Returns JSON to stdout on success (message id, channel id)
- Returns non-zero exit code + error message on failure

### R3: Claude Code skill (SKILL.md)
- **Status**: `accepted`
- Located at `merlin-bot/.claude/skills/discord/SKILL.md`
- Tells Claude how and when to use the discord_send.py script
- Documents the CLI interface, available actions, and examples
- Includes Discord writing style guidance (short, conversational, no markdown tables)

### R4: Environment configuration
- **Status**: `accepted`
- `merlin-bot/.env` holds `DISCORD_BOT_TOKEN` (gitignored)
- `merlin-bot/.env.example` as a template (committed)

### R5: Merlin bot CLAUDE.md
- **Status**: `deferred`
- Merlin's personality and behavioral directives — will be its own epic

## Out of Scope (for now)

- Media/file attachments
- Threads (create, reply in thread)
- Polls, pins, search
- Channel/role management
- The Discord bot listener (`merlin.py`) — separate epic
