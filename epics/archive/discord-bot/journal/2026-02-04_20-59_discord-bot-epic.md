# 2026-02-04 — Discord Bot Epic (T1–T8)

## What was done

Implemented the full discord-bot epic in a single session:

### Batch 1: T1 + T2 + T5 (+ T3, T4, T6 folded in)

- **T1 merlin.py**: Created `merlin-bot/merlin.py` with PEP 723 deps (`discord.py`, `python-dotenv`). Implements `on_ready`, `on_message` with bot filtering, channel filtering, rich prompt building, and `invoke_claude()` call via `asyncio.to_thread`.
- **T2 channel config**: Added `DISCORD_CHANNEL_IDS` to `.env.example` and `.env`. merlin.py parses comma-separated channel IDs.
- **T3 session management**: `session_id_for_channel()` derives `discord-channel-{id}` — built into merlin.py.
- **T4 logging**: Python logging to `logs/merlin.log` + console. Logs: bot ready, incoming messages (author, channel, content preview), Claude results (exit code, duration), errors.
- **T5 CLAUDE.md**: Created `merlin-bot/CLAUDE.md` with Merlin's personality, core directive (always use Discord skill), prompt format docs, react vs reply guidance, Discord writing style.
- **T6 error handling**: try/except around `invoke_claude()`, logs errors, never crashes the bot.

### T7: Unit tests

16 pytest tests in `merlin-bot/tests/test_merlin.py`, all passing:
- `TestBuildPrompt` (5 tests): author, channel ID, message ID, content, exact format
- `TestSessionId` (3 tests): deterministic, integer input, different channels
- `TestOnMessage` (8 tests): ignores bots, ignores unconfigured channels, processes configured channels, correct prompt, correct session ID, exception handling, nonzero exit handling

### T8: Live validation

Started `merlin.py` — bot connected as `Merlin#0000` to guild "My Server". Confirmed:
- Bot ready log: `Bot ready as Merlin#0000 | guilds: ['My Server']`
- Channel listening: `Listening in channels: {'YOUR_CHANNEL_ID'}`
- Sent test message via `discord_send.py` — bot correctly ignored it (sent by bot token, so `author.bot = True`)
- Log file created at `merlin-bot/logs/merlin.log` with proper formatting
- Error handling verified via unit tests (RuntimeError and nonzero exit both handled gracefully)

## Files created/modified

- `merlin-bot/merlin.py` (new) — bot listener
- `merlin-bot/CLAUDE.md` (new) — bot personality
- `merlin-bot/.env.example` (modified) — added DISCORD_CHANNEL_IDS
- `merlin-bot/.env` (modified) — added DISCORD_CHANNEL_IDS
- `merlin-bot/tests/test_merlin.py` (new) — 16 unit tests
- `epics/discord-bot/tasks.md` (modified) — all tasks marked done
- `epics/discord-bot/journal/` (new) — this entry

## Decisions

- T3, T4, T6 were implemented directly in merlin.py alongside T1 rather than as separate passes — they're tightly coupled and doing them separately would have been artificial.
- Used `asyncio.to_thread` for `invoke_claude()` to avoid blocking the event loop.
- Logging goes to both file and console (DEBUG level to file, INFO to console).

## Next steps

- Epic is complete — move to `epics/archive/`
- Future: thread-based sessions, DM support, cron jobs, media attachments
