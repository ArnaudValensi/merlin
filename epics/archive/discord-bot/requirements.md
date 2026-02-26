# Discord Bot — Requirements

## Goal

Create the Discord bot listener (`merlin.py`) and Merlin's bot personality (`merlin-bot/CLAUDE.md`) so that Merlin is a live bot that responds to messages in Discord.

## Context

The wrapper (`claude_wrapper.py`) and Discord skill (`discord_send.py` + SKILL.md) are already built. This epic creates the last piece: the always-running process that listens for Discord messages and feeds them to Claude via the wrapper.

## Requirements

### R1: Discord bot listener (merlin.py)
- **Status**: `accepted`
- Always-running Python script using discord.py
- PEP 723 inline dependencies, run with `uv run merlin.py`
- Connects to Discord using `DISCORD_BOT_TOKEN` from `merlin-bot/.env`
- Listens for messages in configured channel(s)
- On message:
  - Ignore messages from bots (including itself)
  - Build a rich prompt with context: author name, channel ID, message ID
  - Call `invoke_claude()` from `claude_wrapper.py`
  - Claude handles the reply via the Discord skill (merlin.py does NOT send replies)
- One Claude session per channel (session ID derived from channel ID)
- Logs incoming messages (author, channel, content) to `merlin-bot/logs/merlin.log`

### R2: Channel configuration
- **Status**: `accepted`
- The bot should only respond in specific channel(s), not every channel it can see
- Channel IDs configured via environment variable or a simple config
- Default channel: configured via `DISCORD_CHANNEL_IDS`

### R3: Rich prompt format
- **Status**: `accepted`
- The prompt sent to Claude should include:
  - Author display name
  - Channel ID (so Claude knows where to reply)
  - Message ID (so Claude can reply to the specific message or react)
  - The message content
- Example format:
  ```
  [Discord message from "username" in channel 1234567890123456789, message ID 123456789]
  The actual message content here
  ```

### R4: Session management
- **Status**: `accepted`
- One persistent Claude session per channel
- Session ID derived deterministically from channel ID (e.g. `discord-channel-{channel_id}`)
- This gives conversation continuity — Claude remembers prior messages in the channel
- No thread support for now (deferred to future epic)

### R5: Bot personality (merlin-bot/CLAUDE.md)
- **Status**: `accepted`
- This is the CLAUDE.md that Claude Code reads when running as Merlin
- Should contain:
  - Merlin's personality and tone (helpful, concise, conversational)
  - Instructions to always use the Discord skill to respond (never just print text)
  - The default channel ID
  - Guidance on when to react vs reply
  - Discord writing style (short messages, no tables, match the energy)

### R6: Graceful operation
- **Status**: `accepted`
- Handle disconnects/reconnects gracefully (discord.py does this by default)
- Log errors but don't crash on individual message failures
- If Claude invocation fails, log the error (don't send error messages to Discord unless useful)

## Out of Scope

- Thread-based sessions (future epic)
- DM support (future epic)
- Cron jobs (future epic)
- Media attachments (future epic)
- Cost controls / rate limiting (future epic)
