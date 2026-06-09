# Discord Bot

Reference documentation for the Discord bot listener (`merlin_bot.py`) and Discord message delivery.

## Overview

Merlin Bot is a **Discord handler**: it listens for messages via `discord.py`, creates threads for each conversation, builds rich prompts, invokes the configured engine via `lib/engine.py`, and delivers the engine's text output to Discord.

The engine is a black box — it has no notion of Discord. It receives a prompt with conversation history and returns text. The bot handler captures the output and sends it to the appropriate Discord thread via `discord_send.py`.

Contextual system-prompt content arrives by recipe: the bot selects the **managed-assistant** recipe from `lib/agent_context.py` (brain doc + personality + user memory + the Discord overlay `discord_directives.md`) and passes the composed text into `invoke()`.

> **Note**: Cron scheduling is a separate core module (`cron/`), not part of merlin-bot. See [`docs/cron-system.md`](cron-system.md).

```
User sends message in Discord
  → on_message() filters (bot, system, allowlist)
  → _resolve_session() (create thread if needed, get session ID)
  → Transcribe voice if applicable
  → build_prompt() (metadata + content)
  → invoke() via lib/engine.py (loads history, calls engine)
  → Bot sends result.content to Discord thread
  → Bot renames thread if new conversation
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

The `[New thread]` tag is included in the prompt for context. Thread renaming is handled by the bot handler (using `_generate_thread_title()` on the engine's response), not by the engine.

### Metadata Included

- **Author name** — for personality context
- **Thread ID** — identifies the conversation
- **Channel ID** — parent channel
- **Message ID** — the specific message being responded to

## Response Delivery

After the engine returns, the bot handler delivers the response:

1. **Send to thread**: `result.content` is sent to the Discord thread via `discord_send.py`
2. **Chunking**: Messages over 1900 chars are split into multiple Discord messages (splits on newlines, then hard-cuts)
3. **Thread renaming**: For new threads (`is_new_thread=True`), the bot generates a short title from the first line of the response
4. **Reactions**: 🤔 while processing, ✅ on success, ❌ on error

The engine never calls `discord_send.py` itself. It returns text, the bot delivers it.

## Voice Transcription

When a message has the voice flag and audio attachments:

1. Download the `.ogg` file
2. Transcribe via `faster-whisper` (using `transcribe.py`)
3. Post transcription back to thread for user verification
4. Replace message content with transcription in prompt

Processing indicators:
- Add 🎤 reaction immediately
- Replace with 🤔 during engine processing
- Final: ✅ or ❌

## Processing Indicators

| Emoji | Meaning |
|-------|---------|
| 🤔 | Engine is processing |
| ✅ | Success |
| ❌ | Error |

Reaction updates are wrapped in try/except to handle Discord API failures gracefully.

## discord_send.py — REST Transport (CLI: merlin chat)

Standalone script for sending messages via Discord REST API. Used by the bot handler to deliver engine responses, and by `notify.py` for cron notifications.

### Commands

```bash
# Send a message
merlin chat send --channel <id> --content "text" [--file path...] [--thread-on-chunk]

# Reply to a message
merlin chat reply --channel <id> --message <id> --content "text" [--file path...]

# React to a message
merlin chat react --channel <id> --message <id> --emoji "emoji"

# Rename a thread
merlin chat rename-thread --thread <id> --name "New title"
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

### File Attachments

```bash
merlin chat send --channel <id> --file screenshot.png --content "Here's the result"
merlin chat send --channel <id> --file a.png --file b.png
```

Sent via multipart/form-data. Files attached to first chunk.

## Error Handling

Errors at each stage are caught independently:

1. **Session resolution** → ❌ reaction, log to engine-log.jsonl
2. **Voice transcription** → fallback to `[transcription failed]`, continue processing
3. **Engine invocation** → ❌ reaction, log exception
4. **Response delivery** → log warning (non-fatal)
5. **Reaction updates** → silent catch (non-fatal)

The bot never crashes from a single message failure.

## Files

| File | Purpose |
|------|---------|
| `merlin_bot.py` | `on_message()` handler, session resolution, prompt building, response delivery, EXTENSION_META |
| `discord_send.py` | REST transport (send/reply/react/rename) — used by bot handler, notify.py, and `merlin chat` |
| `discord_directives.md` | Canonical Discord style overlay (injected via the managed-assistant recipe) |
| `session_registry.py` | Thread/message → session mapping |
| `transcribe.py` | Voice message transcription |
| `lib/engine.py` | AgentEngine abstraction — invoked by the bot handler |
| `lib/session.py` | JSONL session manager — conversation history |
