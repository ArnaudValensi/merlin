# Discord Bot — Tasks

## T1: Create merlin.py bot listener
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: —
- **Description**: Create `merlin-bot/merlin.py` with PEP 723 inline deps (`discord.py`, `python-dotenv`). Implement:
  - Connect to Discord using `DISCORD_BOT_TOKEN` from `.env`
  - `on_ready` event: log bot name and connected guilds
  - `on_message` event:
    - Ignore messages from bots (including itself)
    - Ignore messages not in configured channel(s)
    - Build rich prompt with author name, channel ID, message ID, content
    - Call `invoke_claude()` from `claude_wrapper.py` with `caller="discord"`
  - Run with `uv run merlin.py`
  - Enable `message_content` intent

## T2: Channel configuration
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: —
- **Description**: Add `DISCORD_CHANNEL_IDS` to `.env.example` (comma-separated list of channel IDs). merlin.py loads this and only responds in those channels. Default: configured via `DISCORD_CHANNEL_IDS`. Update `.env` with the value.

## T3: Session management
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: T1
- **Description**: Derive a deterministic session ID per channel (e.g. `discord-channel-{channel_id}`). Pass it to `invoke_claude()` via the `session_id` parameter. This gives Claude conversation continuity within a channel.

## T4: Logging
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: T1
- **Description**: Set up Python logging in merlin.py:
  - Log to `merlin-bot/logs/merlin.log` (rotating or append)
  - Log: incoming messages (author, channel, content preview), bot ready event, errors
  - Log Claude invocation results (exit code, duration) — the wrapper already logs the full detail

## T5: Create merlin-bot/CLAUDE.md
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: —
- **Description**: Create `merlin-bot/CLAUDE.md` — the personality and instructions that Claude Code reads when running as Merlin. Contents:
  - Merlin's personality: helpful, concise, conversational, a bit witty
  - Core directive: always use the Discord skill to respond (never just print text)
  - Prompt format: explain the rich context format so Claude knows how to parse channel/message IDs
  - When to react vs reply (react for acknowledgements, reply for actual responses)
  - Discord writing style (short, no tables, no headers, match the energy)
  - Default channel ID for reference

## T6: Error handling
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: T1, T3, T4
- **Description**: Wrap the Claude invocation in try/except:
  - If `invoke_claude()` returns non-zero exit code, log the error
  - If the wrapper throws an exception, log it and continue (don't crash the bot)
  - Don't send error messages to Discord unless they'd be useful to the user
  - Ensure one bad message doesn't kill the bot loop

## T7: Unit tests
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: T1, T2, T3, T4, T6
- **Description**: Write pytest tests for merlin.py. Mock discord.py and invoke_claude:
  - Bot ignores its own messages
  - Bot ignores messages from other bots
  - Bot ignores messages in non-configured channels
  - Bot processes messages in configured channels
  - Rich prompt format is correct (author, channel ID, message ID, content)
  - Session ID is derived from channel ID
  - Errors in Claude invocation don't crash the bot

## T8: Live validation
- **Status**: `done`
- **Assignee**: —
- **Dependencies**: T5, T7
- **Description**: Start merlin.py, send messages in Discord, verify:
  - Merlin responds via the Discord skill
  - Merlin ignores messages in other channels
  - Merlin remembers context within a channel session
  - Merlin handles errors gracefully (e.g. send a malformed message)
  - Logs are written correctly
  - Document results in journal entry
