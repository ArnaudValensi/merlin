# Single-Process Unified Plugin Model

## Goal

Consolidate Merlin from two processes (`main.py` + `merlin_bot.py`) into a single process where `main.py` runs everything: dashboard, tunnel, Discord bot, and cron scheduler. The bot becomes a proper package plugin discovered via `merlin-bot/__init__.py`.

## Why

- `restart.sh` currently juggles two processes with fragile `pkill -f` patterns
- If one process dies, the other keeps running in a zombie state
- Tunnel URL notification shells out to `discord_send.py` via subprocess — could be a direct function call
- `BOT_START_TIME` is a disconnected module-level variable — could be set directly
- The old architecture (pre-OS-branch) already ran everything on one event loop and it worked fine

## Design

### Unified plugin contract

`merlin-bot/__init__.py` becomes the single discovery point, re-exporting everything main.py needs:

```python
# Routes (from merlin_app.py)
router          # APIRouter with monitoring pages + API endpoints
NAV_ITEMS       # sidebar nav items
STATIC_DIR      # None (uses root statics)

# Async (from merlin_bot.py)
start()         # coroutine — starts Discord client + cron scheduler
on_tunnel_url() # callback — sends tunnel URL to Discord

# Config
validate()      # check bot config, raise SystemExit on errors
```

This mirrors the existing core module pattern (`files/__init__.py`, `terminal/__init__.py`, etc.) but extended with async tasks.

### How main.py discovers and uses the plugin

**Discovery (at import time):**
```python
try:
    sys.path.insert(0, str(MERLIN_BOT_DIR))
    import merlin_bot as bot_plugin

    app.include_router(bot_plugin.router, dependencies=[Depends(require_auth)])
    nav_items[:0] = bot_plugin.NAV_ITEMS
    show_bot_status = True
except ImportError:
    bot_plugin = None
    logger.info("Merlin Bot not found — running core modules only")
```

**Startup (at runtime):**
```python
async def _run():
    tasks = [asyncio.create_task(server.serve())]

    if TUNNEL_ENABLED:
        on_url = bot_plugin.on_tunnel_url if bot_plugin else None
        tasks.append(asyncio.create_task(start_tunnel(..., on_url=on_url)))

    if bot_plugin:
        tasks.append(asyncio.create_task(bot_plugin.start()))

    await asyncio.gather(*tasks)
```

### Key technical detail: client.run() vs client.start()

`merlin_bot.py` currently uses `client.run(token)` which is a **blocking sync wrapper** that creates its own event loop. This can't work inside main.py's existing `asyncio.run()`.

The fix: add an `async def start_bot()` function that uses `client.start(token)` (the async version). The `on_ready` handler and cron scheduler setup move into `start_bot()`. The existing `main()` + `if __name__ == "__main__"` stays for standalone dev execution.

### Tunnel URL notification

Currently: main.py shells out to `discord_send.py` via `subprocess.run()`.
After: the plugin exports `on_tunnel_url(url)` which calls `discord_send.send_message()` directly (httpx-based, same process).

### Validation

- main.py's `_validate_config()` — core checks (tmux, cloudflared, .env) — unchanged, always runs
- `bot_plugin.validate()` — bot checks (token, channel IDs, claude, uv, ffmpeg) — only if plugin discovered. Fatal errors = SystemExit (misconfigured bot should fail fast, not silently run without it)

### Dependencies

main.py's PEP 723 inline deps gain: `discord.py`, `httpx`, `faster-whisper`

The user confirmed they're OK with all deps in one place. `uv run main.py` installs everything. If `merlin-bot/` isn't present, the import fails gracefully and the dashboard runs alone.

### Standalone bot execution preserved

`merlin_bot.py` keeps its `main()` + `if __name__ == "__main__"` block for dev/testing. Running `cd merlin-bot && uv run merlin_bot.py` still works — it uses `client.run()` (blocking) with its own event loop.

## Acceptance criteria

1. `uv run main.py` starts dashboard + tunnel + bot + cron in one process
2. Removing `merlin-bot/` from sys.path → dashboard works alone, no errors
3. `cd merlin-bot && python merlin_bot.py` still works standalone
4. All 539 tests pass (192 root + 347 bot)
5. `restart.sh` manages one process instead of two
6. Tunnel URL notification reaches Discord without subprocess
7. No regressions in live behavior (dashboard pages, bot responses, cron jobs)
