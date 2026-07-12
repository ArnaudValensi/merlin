# Session Management

Reference documentation for the session system that maps conversations to persistent JSONL transcripts.

## Overview

Every conversation maps 1:1 to a session. Sessions are identified by UUIDs and persisted as JSONL files at `~/.merlin/sessions/<session_id>.jsonl`. The system uses **deterministic IDs** (UUID5) so the same thread always maps to the same session, enabling conversation continuity across restarts.

Merlin manages its own conversation history — no reliance on any engine's built-in session/resume mechanism. On each invocation, the full history is loaded from the JSONL file, optionally compacted, and sent to the engine.

## Session ID Generation

UUID5 patterns, all using `uuid.NAMESPACE_DNS`:

| Context | Pattern | Example Input |
|---------|---------|---------------|
| Thread message | `uuid5(DNS, f"discord-thread-{thread_id}")` | `discord-thread-1469102037017952367` |
| Job | `uuid5(DNS, f"job-{job_id}")` | `job-daily-digest` |
| Channel message (legacy, unused) | `uuid5(DNS, f"discord-channel-{channel_id}")` | channel messages create a thread and use the thread pattern; `session_id_for_channel` has no production caller |

Ephemeral jobs use `uuid4()` instead (fresh session each run).

## Session Storage (JSONL)

**Location**: `~/.merlin/sessions/<session_id>.jsonl`

Each session is a JSONL file (one JSON object per line):

```jsonl
{"v":1,"session_id":"abc-123","created_at":"2026-03-23T10:00:00+00:00","engine":"claude-code","model":"claude-opus-4-6"}
{"role":"user","content":"Check the weather","ts":"2026-03-23T10:00:01+00:00","caller":"job-weather"}
{"role":"assistant","content":"It's 18°C in Paris.","ts":"2026-03-23T10:00:05+00:00","duration":3.2,"tokens_in":150,"tokens_out":42,"cost_usd":0.01}
```

### Turn Roles

| Role | When | Key fields |
|------|------|------------|
| `system` | System prompt | `content` |
| `user` | User/job prompt | `content`, `caller` |
| `assistant` | Engine response | `content`, `duration`, `tokens_in`, `tokens_out`, `cost_usd` |
| `tool_call` | Engine called a tool | `name`, `input` |
| `tool_result` | Tool returned a result | `name`, `output` |
| `compaction` | Turns were dropped | `dropped` (count) |

### Session Manager (`lib/session.py`)

| Function | Purpose |
|----------|---------|
| `create_session(session_id, engine, model)` | Create JSONL file with header |
| `load_session(session_id)` | Read all turns (excluding header) |
| `append_turn(session_id, turn)` | Append one turn (file-locked) |
| `session_exists(session_id)` | Check if session file exists |
| `load_session_header(session_id)` | Read just the header |
| `compact_history(history, max_tokens, keep_recent)` | Drop oldest turns if over limit |

## Session Registry

**File**: `data/session_registry.json`

Maps Discord thread IDs and message IDs to session IDs:

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
           ├─ Found → use that session (job continuation)
           └─ Not found → derive session_id = uuid5(DNS, f"discord-thread-{thread_id}")
  → Register if new
  → Return (thread_id, parent_id, session_id, is_new_thread=False)
```

## Invocation Flow

When `lib/engine.invoke()` is called with a session_id:

1. Load session history from `~/.merlin/sessions/<session_id>.jsonl`
2. Assemble the system prompt from caller-provided parts (callers compose
   brain/personality/user via `lib/agent_context.py` recipes)
3. Call `engine.invoke(prompt, history=history, system_prompt=...)`
4. Engine receives full conversation history and produces a response
5. Append user turn + assistant turn to session JSONL
6. Return `AgentResult` to caller

The engine is stateless — it receives the full context on every call. Session continuity comes from Merlin replaying the history, not from any engine-side session storage.

## Compaction

When history exceeds `engine.context_window * 0.8` (estimated via chars/4 heuristic):

1. Keep the system prompt (first turn if `role == "system"`)
2. Keep the last N turns (default: 20)
3. Drop everything in between
4. Insert a `{"role": "compaction", "dropped": N}` marker

## Job Sessions

- **Non-ephemeral**: `uuid5(DNS, f"job-{job_id}")` — same session across all runs. Engine receives full history of previous executions.
- **Ephemeral** (default): `uuid4()` — fresh session each run. Used for stateless tasks.

## MERLIN_SESSION_ID Environment Variable

Set by `lib/engine.py` when invoking the engine:

```python
env["MERLIN_SESSION_ID"] = session_id
```

Available to child processes for session tracking.

## Session Viewer

The session viewer (`/bot` → Logs → View session) supports two formats:

- **Merlin JSONL** (new): detected by `"v"` key in header. Renders role-based turns (user, assistant, tool_call, tool_result, compaction).
- **Legacy stream-json**: Claude Code's NDJSON format. Renders type-based events (system/init, assistant, user, result).

## Files

| File | Purpose |
|------|---------|
| `lib/session.py` | Session CRUD, compaction |
| `lib/engine.py` | Loads history, records turns, invokes engine |
| `session_registry.py` | Thread/message → session mapping |
| `merlin_bot.py` | Session resolution logic (`_resolve_session()`) |
| `job/runner.py` | `session_id_for_job()` — deterministic job sessions |
