# Session Management

Reference documentation for the session system that maps Discord threads to Claude Code sessions.

## Overview

Every conversation maps 1:1 to a Claude Code session. Sessions are identified by UUIDs and persisted in a registry file. The system uses **deterministic IDs** (UUID5) so the same thread always maps to the same session, enabling memory continuity across bot restarts.

## Session ID Generation

Three UUID5 patterns, all using `uuid.NAMESPACE_DNS`:

| Context | Pattern | Example Input |
|---------|---------|---------------|
| Channel message | `uuid5(DNS, f"discord-channel-{channel_id}")` | `discord-channel-1234567890123456789` |
| Thread message | `uuid5(DNS, f"discord-thread-{thread_id}")` | `discord-thread-1469102037017952367` |
| Cron job | `uuid5(DNS, f"cron-job-{job_id}")` | `cron-job-daily-digest` |

Ephemeral cron jobs use `uuid4()` instead (fresh session each run).

## Session Registry

**File**: `merlin-bot/data/session_registry.json`

Maps thread IDs and message IDs to session IDs:

```json
{
  "threads": {
    "1469102037017952367": "a1b2c3d4-..."
  },
  "messages": {
    "1469102040000000000": "a1b2c3d4-..."
  }
}
```

### Operations (`session_registry.py`)

| Function | Purpose |
|----------|---------|
| `get_thread_session(thread_id)` | Look up session for a thread |
| `set_thread_session(thread_id, session_id)` | Register thread → session |
| `get_message_session(message_id)` | Look up session for a message |
| `set_message_session(message_id, session_id)` | Register message → session |

Message-to-session mappings are registered by `discord_send.py` when sending messages, using the `MERLIN_SESSION_ID` environment variable.

## Session Resolution Flow

When a Discord message arrives in `merlin_bot.py._resolve_session()`:

### Channel Message (not in a thread)

```
User sends message in channel
  → Create thread from message (REST API)
  → Derive session_id = uuid5(DNS, f"discord-thread-{thread_id}")
  → Register thread_id → session_id in registry
  → Return (thread_id, channel_id, session_id, is_new_thread=True)
```

### Thread Message (in an existing thread)

```
User sends message in thread
  → Check registry for thread_id
     ├─ Found → return registered session_id
     └─ Not found
        → Check if thread starter message has a registered session
           ├─ Found → use that session (cron continuation)
           └─ Not found → derive session_id = uuid5(DNS, f"discord-thread-{thread_id}")
  → Register if new
  → Return (thread_id, parent_id, session_id, is_new_thread=False)
```

## Resume-First Strategy

Claude invocations use a **resume-first** approach via `claude_wrapper.py`:

1. Try `claude --resume <session_id>` — resumes existing session.
2. If fails with "No conversation found" — retry with `claude --session-id <session_id>` — creates new session.

This ensures:
- Existing sessions resume seamlessly (preserving conversation history).
- First interaction in a new thread creates the session automatically.
- Bot restarts don't lose session continuity (same UUID5 = same session).

## MERLIN_SESSION_ID Environment Variable

Set by `claude_wrapper.py` when invoking Claude:

```python
env["MERLIN_SESSION_ID"] = session_id
```

Used by `discord_send.py` to register message → session mappings:

```python
def _register_message(message_id: str) -> None:
    session_id = os.environ.get("MERLIN_SESSION_ID")
    if session_id:
        set_message_session(message_id, session_id)
```

This enables **cron continuation**: when a user replies to a cron job's message, the reply thread inherits the cron's session, allowing Claude to continue with the cron's context.

## Session Lifecycle

```
1. User sends channel message
2. Thread created automatically
3. Session derived from thread ID (deterministic)
4. Session registered in registry
5. Claude invoked with session ID (resume-first)
6. Claude responds → discord_send.py registers message → session
7. User replies in thread → same session continues
8. Bot restarts → same thread ID → same session ID → conversation resumes
```

## Cron Job Sessions

- **Non-ephemeral**: `uuid5(DNS, f"cron-job-{job_id}")` — same session across all runs. Claude remembers previous executions.
- **Ephemeral**: `uuid4()` — fresh session each run. Used for stateless tasks (wake-up pings).

When a cron job sends a message, the message → session mapping is registered. If a user replies to that message (creating a thread), the thread inherits the cron's session ID.

## Key Files

| File | Purpose |
|------|---------|
| `session_registry.py` | Registry CRUD operations |
| `data/session_registry.json` | Persistent storage |
| `merlin_bot.py` | Session resolution logic (`_resolve_session()`) |
| `claude_wrapper.py` | Sets `MERLIN_SESSION_ID`, resume-first logic |
| `discord_send.py` | Registers message → session on send |
| `cron_runner.py` | `session_id_for_job()` — deterministic cron sessions |
