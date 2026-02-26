# 2026-02-22 — Project Restructure: Portable Dev Environment

## Summary

Completed the full restructure of Merlin from a Discord-bot-with-dashboard into a portable mobile dev environment. The core insight: the file browser, web terminal, git viewer, and notes editor are the real value — the Discord bot is an optional app that plugs in.

## Architecture Change

**Before:**
```
merlin-bot/
├── merlin.py          # Entry point: bot + dashboard + tunnel + cron
├── dashboard.py       # FastAPI app with all routes
├── auth.py, tunnel.py
├── files/, terminal/, commits/, notes/
└── static/, templates/
```

**After:**
```
merlin/
├── main.py            # Entry point: FastAPI + tunnel (core)
├── restart.sh         # Restart both processes
├── auth.py, tunnel.py # Shared infrastructure
├── files/, terminal/, commits/, notes/  # Core modules
├── static/, templates/
├── .env               # Dashboard config (DASHBOARD_PASS, TUNNEL_*)
├── tests/             # Core module tests (auth, tunnel, files, commits, terminal)
└── merlin-bot/
    ├── merlin_bot.py  # Entry point: Discord bot + cron only
    ├── merlin_app.py  # App plugin: exports router + nav items
    ├── .env           # Bot config (DISCORD_BOT_TOKEN, etc.)
    └── tests/         # Bot-specific tests
```

## Commits (12)

### Phase 1: Core restructure

1. **`396c610` Restructure Merlin** — The big move. 48 files changed. Moved core modules to root, created `main.py` entry point, dynamic sidebar via template context injection, `merlin_app.py` as the bot's plugin interface. 539 tests passing.

2. **`08db10a` Move restart.sh, update alias** — Script now at project root. Updated `.zshrc` aliases: `merlin`, `mtest` (root), `mbtest` (bot).

3. **`f62e17f` Split .env** — Dashboard vars (`DASHBOARD_PASS`, tunnel config) at root `.env`. Bot vars (`DISCORD_BOT_TOKEN`, etc.) stay in `merlin-bot/.env`. Both have `.env.example` files.

4. **`0ca4d0e` Fail-fast validation in main.py** — Validates `.env` existence, `tmux`, `cloudflared` (if tunnel enabled), `DASHBOARD_PASS` (auto-generates for tunnel, warns for local).

5. **`be94c14` Clean up merlin_bot.py** — Removed 97 lines of dead code: dashboard vars, `_start_dashboard()`, `_start_tunnel_task()`, `_notify_tunnel_url()`, and dashboard/tunnel validation. Bot now only validates its own config.

6. **`3e666a9` Rename merlin.py → merlin_bot.py** — Clearer naming. Updated all 18 files with references. Test file renamed to `test_merlin_bot.py`.

7. **`9af64a5` Remove dashboard.py** — Fully replaced by `merlin_app.py`. Deleted 514 lines. `test_dashboard.py` now imports `merlin_app`. `test_tunnel.py` moved to root tests.

8. **`8a7e99f` Journal entry** — This file (initial version).

### Phase 2: Post-deploy fixes

9. **`e293cda` Tunnel URL Discord notification** — `main.py` now calls `discord_send.py` when the tunnel connects. Replaced the `_notify_tunnel_url` that was removed from `merlin_bot.py`.

10. **`3a6cb19` Fix notification + add logging** — The notification wasn't firing because `logging.basicConfig()` was missing (tunnel logs were silent) and `subprocess.Popen` swallowed errors. Fixed with `subprocess.run` and proper error reporting.

11. **`6fb2493` Kill stale cloudflared on restart** — `restart.sh` now kills `cloudflared tunnel` processes. Previously, killing `main.py` orphaned its cloudflared child, leaving stale tunnels.

12. **`00e5149` Fix restart.sh kill patterns** — `uv run` spawns `python` (not `python3`) on some systems. The old `pkill -f "python3 merlin_bot.py"` missed running instances, causing **duplicate bot instances** on restart. Both bots connected to Discord and processed the same message twice. Fixed by matching both `uv run <script>` and `python <script>`.

## Key Design Decisions

- **App plugin pattern**: Apps export `router`, `NAV_ITEMS`, `STATIC_DIR`. `main.py` discovers via simple `try: import`. No registry, no config — just presence detection.
- **Template context injection**: `_patch_template_responses()` monkey-patches `Jinja2Templates.TemplateResponse` to inject `nav_items` and `show_bot_status` into all template contexts across all modules.
- **Auth decoupling**: `auth.py` uses a `configure(password)` function instead of importing from dashboard (broke circular dependency).
- **CWD = launch directory**: Each module has `set_cwd()` called by `main.py` at startup. No UI for switching (v1).
- **Two entry points, two .env files**: Clean separation of concerns. Each process validates only what it owns.

## Bugs Found During Deploy

1. **Stale cloudflared**: Killing `main.py` doesn't kill its cloudflared child — becomes orphan. Fix: `restart.sh` explicitly kills `cloudflared tunnel`.
2. **Duplicate bot instances**: `pgrep -f "python3 merlin_bot.py"` doesn't match `python merlin_bot.py` (uv uses `python` not `python3`). Old bot stays alive, new bot starts → two Discord connections → duplicate message processing. Fix: match on `uv run <script>` and `python <script>`.
3. **Silent tunnel failure**: No `logging.basicConfig()` meant tunnel and merlin loggers produced no output. Tunnel connected but `on_url` callback errors were invisible. Fix: added `logging.basicConfig()` in `main.py`.

## Validation

- **Tests**: 347 root + 347 bot passing
- **Visual**: Screenshots at desktop + mobile for all pages. Sidebar shows all 7 nav items with bot status dot.
- **Startup**: Fail-fast tested — missing `.env`, missing password, local-only mode all work correctly.
- **Live deploy**: Dashboard + tunnel + bot running, tunnel URL notification sent to Discord.

## What's Left

- Push to remote and merge to master
- Consider adding more apps in the future (the plugin pattern is ready)
