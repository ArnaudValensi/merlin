# Context — Single-Process Epic

## Key Code References

### Core module plugin pattern (the model to follow)

Each core module has an `__init__.py` that exports router + static dir:

**`files/__init__.py`:**
```python
from .routes import router, FILES_STATIC_DIR
__all__ = ["router", "FILES_STATIC_DIR"]
```

**`terminal/__init__.py`:**
```python
from .routes import router
__all__ = ["router"]
```

main.py imports from the package name:
```python
from files import router as files_router, FILES_STATIC_DIR
from terminal import router as terminal_router
```

### Current bot discovery (two separate imports — to be unified)

**main.py lines 226-242** — discovers merlin_app.py for routes:
```python
try:
    sys.path.insert(0, str(MERLIN_BOT_DIR))
    from merlin_app import merlin_app_router, MERLIN_APP_NAV_ITEMS, MERLIN_APP_STATIC_DIR
    app.include_router(merlin_app_router, dependencies=[Depends(require_auth)])
    nav_items[:0] = MERLIN_APP_NAV_ITEMS
    show_bot_status = True
    _merlin_bot_loaded = True
except ImportError:
    logger.info("Merlin Bot app not found — running core modules only")
```

### Current tunnel URL notification (to be replaced)

**main.py lines 368-389** — shells out to discord_send.py:
```python
async def _notify_tunnel_url(url: str) -> None:
    if not _merlin_bot_loaded:
        return
    import subprocess
    channel = os.getenv("DISCORD_CHANNEL_IDS", "").split(",")[0].strip()
    msg = f"Dashboard is live at {url}"
    result = subprocess.run(
        ["uv", "run", "discord_send.py", "send", "--channel", channel, "--content", msg],
        cwd=str(MERLIN_BOT_DIR), capture_output=True, text=True, timeout=30,
    )
```

### Current async startup (to add bot.start() here)

**main.py lines 391-408:**
```python
async def _run():
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    tasks = [asyncio.create_task(server.serve())]
    if TUNNEL_ENABLED:
        from tunnel import start_tunnel
        tasks.append(asyncio.create_task(start_tunnel(..., on_url=_notify_tunnel_url)))
    await asyncio.gather(*tasks)
asyncio.run(_run())
```

### merlin_bot.py entry point (to be refactored)

**merlin_bot.py lines 508-531:**
```python
def main() -> None:
    _validate_config()

    @client.event
    async def on_ready() -> None:
        if not hasattr(client, "_ready_done"):
            client._ready_done = True
            guilds = [g.name for g in client.guilds]
            logger.info("Bot ready as %s | guilds: %s", client.user, guilds)
            log_event("bot_event", event="ready", ...)
            asyncio.create_task(_cron_scheduler())

    client.run(DISCORD_BOT_TOKEN, log_handler=None)

if __name__ == "__main__":
    main()
```

Key: `client.run()` is sync/blocking. Must use `client.start()` (async) for plugin mode.

Note: `client.start()` does NOT accept `log_handler` arg. Discord.py logging must be configured separately (suppress discord.py's default handler before calling start).

### merlin_app.py exports (to be re-exported via __init__.py)

**merlin_app.py lines 35-44:**
```python
merlin_app_router = APIRouter()
MERLIN_APP_STATIC_DIR = None
MERLIN_APP_NAV_ITEMS = [
    {"url": "/overview", "icon": "&#9673;", "label": "Overview"},
    {"url": "/performance", "icon": "&#9632;", "label": "Performance"},
    {"url": "/logs", "icon": "&#9776;", "label": "Logs"},
]
```

### discord_send.py — direct message function

For the tunnel URL callback, use `send_message()` from `discord_send.py` instead of subprocess:
```python
from discord_send import send_message
# send_message(channel_id, content) — uses httpx REST API
```

Check `discord_send.py` for the exact signature.

### merlin_bot.py module-level state

These are created at import time and shared across the module:
- `client = discord.Client(intents=intents)` — line ~240
- `_new_sessions: set[str]` — tracks sessions not yet created
- `_channel_locks: dict[str, asyncio.Lock]` — per-channel concurrency
- `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_IDS` — from .env
- `@client.event async def on_message(...)` — line 251, at module level

The `on_message` handler is at module level (decorator-based). The `on_ready` handler is registered inside `main()`. For the plugin refactor, `on_ready` moves into `start_bot()`.

### PEP 723 deps

**Current main.py:**
```
fastapi, uvicorn[standard], jinja2, python-dotenv, python-multipart
```

**Current merlin_bot.py:**
```
discord.py, python-dotenv, httpx, fastapi, uvicorn[standard], jinja2, faster-whisper, python-multipart
```

**After merge (main.py gets all):**
```
fastapi, uvicorn[standard], jinja2, python-dotenv, python-multipart, discord.py, httpx, faster-whisper
```

### Test commands

```bash
# Root tests (192 tests)
uv run --with pytest --with httpx --with croniter pytest tests/ --ignore=tests/test_touch_scroll.py -v

# Bot tests (347 tests)
cd merlin-bot && .venv/bin/pytest tests/ -v
```

### merlin_bot.py _validate_config() — lines 395-447

Checks: .env exists, DISCORD_BOT_TOKEN set, DISCORD_CHANNEL_IDS set, claude on PATH, uv on PATH, ffmpeg on PATH. Fatal on any error.
