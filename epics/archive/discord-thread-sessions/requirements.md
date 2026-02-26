# Epic: Discord Threads as Claude Sessions

## Overview

Map Discord threads 1:1 to Claude Code sessions. Every user conversation happens in a thread — channel messages create a new thread, thread messages continue the existing session. Cron outputs can be continued by threading on the bot's message.

## Goals

1. **Conversation Isolation** — Each thread is an independent Claude session. No cross-talk between conversations.
2. **Session Persistence** — Thread → session mapping survives bot restarts via a JSON registry.
3. **Cron Continuation** — User can reply to a cron output message, creating a thread that resumes the cron's Claude session with full context.
4. **Clean Channel** — The channel becomes a list of conversation starters. All actual dialogue lives in threads.

## Architecture

### Session Resolution

```
User posts in channel
  → Create thread from message
  → session = uuid5("discord-thread-{thread_id}")
  → Register thread → session

User posts in existing thread
  → Look up session from registry
  → Resume session

User threads on a bot/cron message
  → Look up message → session from registry
  → Resume that session (cron context preserved)
  → Register thread → session for future messages
```

### Session Registry

**File:** `merlin-bot/data/session_registry.json`

```json
{
  "threads": { "<thread_id>": "<session_id>" },
  "messages": { "<bot_message_id>": "<session_id>" }
}
```

- `threads` — Deterministic for user-created threads, cron session ID for cron-originated threads. Only needed for cron continuation (user threads are deterministic), but stored uniformly for simplicity.
- `messages` — Populated by `discord_send.py` via `MERLIN_SESSION_ID` env var. Enables cron continuation.

### Cron Message Tracking

`discord_send.py` doesn't know which session it's running in. Solution:

1. `claude_wrapper.py` sets `MERLIN_SESSION_ID` in the subprocess environment (per-invocation copy, no race conditions)
2. Env var propagates: `claude_wrapper` → `claude` → `discord_send.py`
3. `discord_send.py` writes `message_id → session_id` to registry after every send/reply

### Thread Allowlisting

Threads whose **parent channel** is in `DISCORD_CHANNEL_IDS` are automatically allowed. No new config needed.

### Prompt Format

```
[Discord message from "Alice" in channel {thread_id} (thread in {parent_id}), message ID {msg_id}]
Content here
```

Claude sees the thread ID as "channel" → responds there via `discord_send.py send --channel {thread_id}`. No changes to bot CLAUDE.md needed.

### Reactions

🤔/✅/❌ are placed on the **triggering message** (the user's message that caused the invocation), whether it's in the channel or in a thread.

## Components

| Component | Change | Purpose |
|-----------|--------|---------|
| `session_registry.py` | **New** | JSON-backed registry with file locking |
| `merlin.py` | **Major** | Thread detection, creation, session resolution |
| `claude_wrapper.py` | **Minor** | Set `MERLIN_SESSION_ID` env var |
| `discord_send.py` | **Minor** | Register message → session, add `create_thread` |
| `tests/test_session_registry.py` | **New** | Registry unit tests |
| `tests/test_merlin.py` | **Update** | Thread mock support, new test cases |

## Edge Cases

- **Rapid messages before thread created** — Use `asyncio.Lock` per channel to serialize thread creation
- **Archived threads** — Discord auto-unarchives on message send. Session persists.
- **Thread deletion** — Orphaned registry entry. Harmless.
- **Registry growth** — Append-only for now. Add cleanup later if needed.

## Acceptance Criteria

### Must Have
- [ ] Channel messages create a thread and respond there
- [ ] Thread messages resume the correct session
- [ ] Threading on a cron/bot message resumes the cron session
- [ ] Registry persists across bot restarts
- [ ] Reactions (🤔/✅/❌) appear on the triggering message
- [ ] Threads in allowed channels are processed; others are ignored
- [ ] All new code has unit tests

### Should Have
- [ ] Concurrency guard for rapid channel messages
- [ ] Thread name derived from message content (first ~80 chars)

### Nice to Have
- [ ] Registry cleanup for old/deleted threads
- [ ] Metrics: thread count, session reuse rate
