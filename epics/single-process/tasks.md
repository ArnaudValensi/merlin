# Tasks — Single-Process Unified Plugin Model

## Task 1: Add plugin interface to `merlin_bot.py`

- **Status**: done
- **Description**: Added `start_bot()` (async, uses `client.start()`), `start()`, `on_tunnel_url()`, `validate()`, and re-exports from `merlin_app.py` (router, NAV_ITEMS, STATIC_DIR). Standalone `main()` preserved for dev mode. Note: `__init__.py` approach was abandoned because `merlin-bot/` has a hyphen and can't be imported as a Python package — plugin exports live directly in `merlin_bot.py` since that's what `import merlin_bot` finds.
- **Files**: `merlin-bot/merlin_bot.py`

## Task 2: Refactor `main.py` — unified plugin discovery

- **Status**: done
- **Description**: Replaced `from merlin_app import ...` + `_merlin_bot_loaded` flag with `import merlin_bot as bot_plugin`. Added PEP 723 deps (discord.py, httpx, faster-whisper). Wired up routes, tunnel callback (direct function call instead of subprocess), and `bot_plugin.start()` in asyncio.gather.
- **Files**: `main.py`

## Task 3: Simplify `restart.sh`

- **Status**: done
- **Description**: Single process now. Removed `merlin_bot.py` start/kill/verify. Only manages `main.py` + `cloudflared tunnel`. Still kills old `merlin_bot.py` processes for clean transition.
- **Files**: `restart.sh`

## Task 4: Update docs

- **Status**: done
- **Description**: Updated CLAUDE.md: architecture diagram (one process), entry point description, component tables, plugin pattern docs, restart.sh description.
- **Files**: `CLAUDE.md`

## Task 5: Run tests and validate

- **Status**: done
- **Description**: Both test suites pass: 192 root + 347 bot = 539 total, zero failures.

```
Root: 192 passed (tests/ --ignore=tests/test_touch_scroll.py)
Bot:  347 passed (merlin-bot/tests/)
```
