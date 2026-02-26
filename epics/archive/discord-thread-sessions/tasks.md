# Tasks: Discord Thread Sessions

## Status Legend
- [ ] Todo
- [x] Done
- [~] In Progress
- [-] Blocked

---

## Phase 1: Session Registry

### 1.1 Create session_registry.py
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: None

Create `merlin-bot/session_registry.py` — JSON-backed registry with:
- `data/session_registry.json` storage
- `get/set_thread_session(thread_id, session_id)`
- `get/set_message_session(message_id, session_id)`
- File locking via `fcntl` for thread safety
- Auto-creates `data/` directory and file on first write

### 1.2 Test session registry
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 1.1

Create `tests/test_session_registry.py`:
- CRUD operations for threads and messages
- Persistence (write, reload, verify)
- Missing file / empty file handling
- Concurrent-safe (file locking)

---

## Phase 2: Environment Variable Plumbing

### 2.1 Pass session ID via environment in claude_wrapper.py
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: None

In `invoke_claude()`, create an env copy with `MERLIN_SESSION_ID` and pass to `subprocess.run(env=env)`.

### 2.2 Register messages in discord_send.py
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 1.1

After `send_message()` and `reply_message()` succeed, read `MERLIN_SESSION_ID` from env. If present, call `set_message_session(message_id, session_id)`.

### 2.3 Add create_thread function to discord_send.py
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: None

Add `create_thread_from_message(channel_id, message_id, name, token)` — calls Discord REST API `POST /channels/{channel_id}/messages/{message_id}/threads`. Returns thread data dict. No CLI subcommand needed (called from merlin.py).

### 2.4 Test discord_send changes
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 2.2, 2.3

Test message registration (mock env var, verify registry write).
Test create_thread (mock HTTP, verify payload).

---

## Phase 3: Thread Handling in merlin.py

### 3.1 Add thread detection and allowlisting
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: None

In `on_message()`:
- Detect threads via `isinstance(message.channel, discord.Thread)`
- For threads: check `parent_id` against `DISCORD_CHANNEL_IDS`
- For channels: check `channel_id` as before

### 3.2 Add session_id_for_thread helper
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: None

```python
def session_id_for_thread(thread_id: str | int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"discord-thread-{thread_id}"))
```

Keep `session_id_for_channel()` for backward compatibility.

### 3.3 Rework on_message for thread-first flow
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 1.1, 2.3, 3.1, 3.2

Core logic rewrite:

**Channel message:**
1. Create thread from message (via `create_thread_from_message`)
2. Generate session ID from thread ID
3. Register thread → session

**Thread message:**
1. Look up session from registry (`get_thread_session`)
2. If not found: check `messages` registry for thread starter (cron continuation)
3. If still not found: generate deterministic session, register it

**Both paths then:**
4. React 🤔 on triggering message
5. Invoke Claude (resume-first, fallback to create)
6. Remove 🤔, add ✅/❌

Rename `_new_channels` → `_new_sessions` (track session IDs).

### 3.4 Update build_prompt for thread context
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 3.1

Update prompt format to include thread context:
```
[Discord message from "Alice" in channel {thread_id} (thread in {parent_id}), message ID {msg_id}]
```

### 3.5 Add concurrency guard for thread creation
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 3.3

Use `asyncio.Lock` per channel to prevent two messages from creating two threads simultaneously.

### 3.6 Test thread handling
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 3.3, 3.4

Update `make_message()` with `is_thread`, `thread_id`, `parent_channel_id` params.

New test cases:
- Channel message creates thread and invokes Claude
- Thread message looks up session from registry
- Thread in allowed parent channel is processed
- Thread in non-allowed parent channel is rejected
- Cron message continuation (registry lookup for starter message)
- Unknown thread gets new deterministic session
- Session IDs differ for thread vs channel with same numeric ID
- Build prompt includes thread context
- Rapid messages serialized (concurrency guard)

---

## Phase 4: Integration & Validation

### 4.1 End-to-end smoke test
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: All above

1. Run full test suite: `cd merlin-bot && .venv/bin/pytest tests/ -v`
2. Manual: send message in Discord → verify thread created, response in thread
3. Manual: reply in thread → verify same session resumed
4. Manual: trigger cron → reply to output → verify cron session continued
5. Manual: restart bot → reply in thread → verify session from registry

### 4.2 Update documentation
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 4.1

- Update `merlin/CLAUDE.md` architecture section
- Add thread session info to `merlin-bot/CLAUDE.md` if needed
- Journal entry summarizing the work

---

## Notes

- Phases 1 and 2 are largely parallelizable
- Phase 3 is the core work and depends on 1 + 2
- Phase 4 validates everything end-to-end
