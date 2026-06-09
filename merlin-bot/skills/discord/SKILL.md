---
name: discord
description: Send messages, reply to messages, and react to messages in Discord. Use this skill whenever you need to communicate back to the user via Discord.
user-invocable: false
allowed-tools: Bash
---

# Discord Skill

Send messages, replies, and reactions to Discord channels using the `merlin chat` command. It works from any directory.

## Usage

### Send a message

```bash
merlin chat send --channel <channel_id> --content "Your message here"
```

### Send with attachments

```bash
# Message with an image
merlin chat send --channel <channel_id> --content "Here's the screenshot" --file screenshot.png

# Just a file (no text)
merlin chat send --channel <channel_id> --file report.pdf

# Multiple files
merlin chat send --channel <channel_id> --file a.png --file b.png --content "Two images"
```

### Reply to a message

```bash
merlin chat reply --channel <channel_id> --message <message_id> --content "Your reply here"

# Reply with attachment
merlin chat reply --channel <channel_id> --message <message_id> --content "Here you go" --file result.png
```

### React to a message

```bash
merlin chat react --channel <channel_id> --message <message_id> --emoji "✅"
```

### `--channel` must contain the message (reply & react)

For `reply` and `react`, `--channel` is **the channel that contains the target message**, not necessarily the channel you received context from.

Discord scopes a message ID to its channel. A thread is its own channel, so a message posted in a thread lives in the **thread's ID**, not the parent channel's. If you target the parent channel while the message is in a thread, the API returns `MESSAGE_REFERENCE_UNKNOWN_MESSAGE` (reply) or `Unknown Message` (react).

**Rule of thumb:** when you're operating inside a thread, pass the **thread ID** as `--channel`. The incoming context gives you both `channel` (the thread) and the parent channel — use the one the message actually lives in.

### Rename a thread

```bash
merlin chat rename-thread --thread <thread_id> --name "Short descriptive title"
```

## Output

- `send` and `reply` print JSON to stdout: `{"message_id": "...", "channel_id": "..."}`
- For chunked messages (content > 2000 chars), a JSON array is returned instead.
- `react` prints `{"ok": true}` on success.
- `rename-thread` prints `{"ok": true, "thread_id": "...", "name": "..."}` on success.
- All commands exit non-zero and print an error to stderr on failure.

## Message chunking

Discord has a 2000-character limit per message. Long messages are automatically split into multiple messages. The splitter prefers breaking at newlines, then spaces, then hard-cuts at 2000 characters.

For `reply`, only the first chunk is sent as an actual reply (with the reply indicator). Subsequent chunks are sent as regular follow-up messages.

### Thread-on-chunk (for main channel messages)

When sending long messages to the **main channel** (not a thread), use `--thread-on-chunk` to preserve session continuity:

```bash
merlin chat send --channel <channel_id> --content "Long message..." --thread-on-chunk
```

This creates a thread from the first message and sends remaining chunks there. The user can then reply in the thread and Merlin will resume with the correct session context.

**Always use `--thread-on-chunk` when sending to the main channel**. Do NOT use it when already sending inside a thread.

## File attachments

Use `--file` to attach files to `send` or `reply`. Supported types:
- **Images** (PNG, JPG, GIF, WEBP) — embedded inline
- **Videos** (MP4, MOV, WEBM) — embedded player
- **Audio** (MP3, OGG, WAV) — embedded player
- **Files** (PDF, TXT, ZIP, etc.) — downloadable attachment

Files are attached to the first message chunk. Use `--file` multiple times for multiple attachments. Either `--content` or `--file` (or both) must be provided.

## Common emoji for reactions

- 🤔 — thinking / processing started
- ✅ — done / success
- ❌ — error / failure
- 👍 — acknowledged

## Discord writing style

Keep messages conversational. Discord is chat, not documentation.

- Short, punchy messages (1-3 sentences)
- Use **bold** for emphasis, `code` for technical terms
- Use lists for multiple items
- No markdown tables (Discord renders them as ugly raw text)
- No `## Headers` in chat (use **bold** instead)
- Skip filler like "I'd be happy to help!"
- Match the energy of the conversation
- Break up long responses into multiple short messages rather than one wall of text
