# Discord Bot

Reference documentation for the Discord bot listener (`merlin_bot.py`) and the Discord skill (`discord_send.py`).

## Overview

Merlin listens for Discord messages via `discord.py`, creates threads for each conversation, builds rich prompts, and invokes Claude Code. Responses are sent back via the `discord_send.py` REST script.

```
User sends message in Discord
  → on_message() filters (bot, system, allowlist)
  → _resolve_session() (create thread if needed, get session ID)
  → Transcribe voice if applicable
  → build_prompt() (metadata + content)
  → invoke_claude() (resume-first)
  → Claude uses discord skill to respond
```

## Message Filtering

In `merlin_bot.py.on_message()`, messages are filtered in order:

1. **Bot messages**: `message.author.bot == True` → ignored (prevents loops)
2. **System messages**: Only `MessageType.default` and `MessageType.reply` processed. Thread creation notices, pins, joins, etc. are ignored.
3. **Channel allowlist**: Only channels in `DISCORD_CHANNEL_IDS` env var. Thread messages check parent channel.

## Thread Creation

**Channel messages** automatically create a thread:

- Thread name: first 80 characters of message content (or "Conversation")
- Auto-archive: 3 days (4320 minutes)
- Created via REST API (`discord_send.py`) rather than discord.py
- Per-channel asyncio lock prevents race conditions on simultaneous messages

**Thread messages** look up the existing session from the registry (see `docs/session-management.md`).

## Rich Prompt Format

### Standard Message

```
[Discord message from "Alice" in thread 14691..., channel 14686..., message ID 14691...]
What's the best way to optimize this shader?
```

### Voice Message

```
[Discord voice message from "Bob" in thread 14691..., channel 14686..., message ID 14691...]
[Transcribed audio]: Can you explain how the cron job system works?
```

### New Thread (first message)

```
[New thread]
[Discord message from "Charlie" in thread 14691..., channel 14686..., message ID 14691...]
Let's build a dashboard
```

The `[New thread]` tag signals Claude to rename the thread after replying.

### Metadata Included

- **Author name** — for personality context
- **Thread ID** — where to send replies
- **Channel ID** — parent channel (for cron job creation reference)
- **Message ID** — for replying/reacting to specific messages

## Voice Transcription

When a message has the voice flag and audio attachments:

1. Download the `.ogg` file
2. Transcribe via `faster-whisper` (using `transcribe.py`)
3. Post transcription back to thread for user verification
4. Replace message content with transcription in prompt

Processing indicators:
- Add 🎤 reaction immediately
- Replace with 🤔 during Claude processing
- Final: ✅ or ❌

## Processing Indicators

| Emoji | Meaning |
|-------|---------|
| 🤔 | Claude is processing |
| ✅ | Success |
| ❌ | Error |

Reaction updates are wrapped in try/except to handle Discord API failures gracefully.

## discord_send.py — REST API Script

Standalone script for sending messages via Discord REST API.

### Commands

```bash
# Send a message
uv run discord_send.py send --channel <id> --content "text" [--file path...] [--thread-on-chunk]

# Reply to a message
uv run discord_send.py reply --channel <id> --message <id> --content "text" [--file path...]

# React to a message
uv run discord_send.py react --channel <id> --message <id> --emoji "emoji"

# Rename a thread
uv run discord_send.py rename-thread --thread <id> --name "New title"
```

### Output Format

- Single message: `{"message_id": "...", "channel_id": "..."}`
- Chunked: `[{"message_id": "...", "channel_id": "..."}, ...]`
- React: `{"ok": true}`
- Rename: `{"ok": true, "thread_id": "...", "name": "..."}`

### Message Chunking

Discord has a 2000-character limit. Long messages are split intelligently:

1. Split at last newline within 2000 chars
2. Split at last space within 2000 chars
3. Hard cut at 2000 chars

**Thread-on-chunk** (`--thread-on-chunk`): For main channel messages, creates a thread from the first chunk and sends remaining chunks there. Always use when sending to the main channel.

**Reply chunking**: First chunk is a reply (with reply indicator), subsequent chunks are regular follow-ups.

### File Attachments

```bash
uv run discord_send.py send --channel <id> --file screenshot.png --content "Here's the result"
uv run discord_send.py send --channel <id> --file a.png --file b.png
```

Sent via multipart/form-data. Files attached to first chunk.

### Session Registration

After sending, `discord_send.py` registers `message_id → session_id` if `MERLIN_SESSION_ID` is set (enables cron continuation).

## Discord Skill

**Location**: `merlin-bot/.claude/skills/discord/SKILL.md`

Tells Claude how and when to use `discord_send.py`. Key conventions:

- **Writing style**: Short, conversational. No markdown tables. No headers in chat.
- **Send vs Reply**: Use `reply` for direct responses, `send` for standalone messages.
- **Thread-on-chunk**: Always use when sending to the main channel.
- **Common reactions**: 🤔 (processing), ✅ (done), ❌ (error), 👍 (acknowledged)

## Error Handling

Errors at each stage are caught independently:

1. **Session resolution** → ❌ reaction, log to structured.jsonl
2. **Voice transcription** → fallback to `[transcription failed]`, continue processing
3. **Claude invocation** → ❌ reaction, log exception
4. **Reaction updates** → silent catch (non-fatal)

The bot never crashes from a single message failure.

## Key Files

| File | Purpose |
|------|---------|
| `merlin_bot.py` | `on_message()` handler, session resolution, prompt building |
| `discord_send.py` | REST API script (send/reply/react/rename) |
| `.claude/skills/discord/SKILL.md` | Claude Code skill definition |
| `session_registry.py` | Thread/message → session mapping |
| `transcribe.py` | Voice message transcription |
