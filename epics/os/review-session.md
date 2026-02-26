# OS Epic — Review Session Context

## What This Is

The `OS` branch contains a major restructure of the Merlin project. It needs a thorough code review before merging to `master`. This document provides all the context needed for a fresh session to review efficiently.

## The Change In One Sentence

Merlin was restructured from a Discord-bot-that-has-a-dashboard into a portable mobile dev environment where the Discord bot is an optional plugin.

## Branch Info

- **Branch**: `OS` (13 commits ahead of `master`)
- **Diff**: 67 files changed, +1198 / -583 lines
- **Tests**: 192 root + 347 merlin-bot = 539 passing
- **Live**: Running in production, tunnel + bot both operational

## What To Review

### 1. New entry point: `main.py` (412 lines)

The core of the restructure. Read this file carefully. Key concerns:
- PEP 723 inline deps (line 1-3)
- Auth configuration flow (line 70-79)
- Nav items and sidebar injection (line 84-99, 253-278)
- Module registration and static mounts (line 197-243)
- Merlin-bot app discovery via `try: import` (line 226-241)
- `_validate_config()` fail-fast checks (line 287-330)
- `_notify_tunnel_url()` Discord callback (line 367-390)
- Async startup with uvicorn + tunnel (line 392-399)

### 2. Moved and modified files

These files were `git mv`'d from `merlin-bot/` to project root and modified:

| File | Key Changes |
|------|-------------|
| `auth.py` | Removed circular import with dashboard. Added `configure(password)` function and module-level `_dashboard_password` var. |
| `files/routes.py` | Added `set_cwd()`, `/files` redirects to CWD path. `PROJECT_ROOT` changed from `MERLIN_BOT_DIR`. |
| `terminal/routes.py` | Added `set_cwd()`, PTY starts in CWD instead of PROJECT_ROOT. |
| `commits/git_parser.py` | Added `set_repo_dir()` to make REPO_DIR configurable. |
| `commits/routes.py` | `PROJECT_ROOT` path fix. |
| `notes/routes.py` | `PROJECT_ROOT` path fix. `MEMORY_DIR` now points to `PROJECT_ROOT / "merlin-bot" / "memory"`. |
| `templates/base.html` | Dynamic sidebar via `nav_items` and `show_bot_status` Jinja2 vars instead of hardcoded nav. |

### 3. Bot-side changes

| File | Key Changes |
|------|-------------|
| `merlin-bot/merlin_bot.py` (was `merlin.py`) | Removed all dashboard/tunnel code: vars, `_start_dashboard()`, `_start_tunnel_task()`, `_notify_tunnel_url()`, related validation. Bot only does Discord + cron now. |
| `merlin-bot/merlin_app.py` (NEW) | Extracted from `dashboard.py`. Exports `merlin_app_router`, `MERLIN_APP_NAV_ITEMS`, `MERLIN_APP_STATIC_DIR`. Contains all monitoring routes (overview, performance, logs, session pages + API endpoints). |
| `merlin-bot/dashboard.py` | DELETED — fully replaced by `merlin_app.py`. |

### 4. Infrastructure

| File | Key Changes |
|------|-------------|
| `restart.sh` (moved from `merlin-bot/`) | Starts `main.py` + `merlin_bot.py` as separate processes. Kills `uv run <script>`, `python <script>`, and `cloudflared tunnel`. |
| `.env` (NEW) | Dashboard vars: `DASHBOARD_USER`, `DASHBOARD_PASS`. |
| `.env.example` (NEW) | Template with tunnel vars commented out. |
| `merlin-bot/.env` | Now only bot vars: `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_IDS`, `CRON_TIMEZONE`. |
| `tests/conftest.py` (NEW) | Adds project root + `merlin-bot/` to `sys.path`. |
| `merlin-bot/tests/conftest.py` | Added project root to `sys.path` (for `tunnel` import via `merlin_app`). |

### 5. Test moves

Tests for moved modules were moved from `merlin-bot/tests/` to `tests/`:
- `test_auth.py`, `test_fs_helpers.py`, `test_commits_routes.py`, `test_git_parser.py`, `test_terminal.py`, `test_touch_scroll.py`, `test_tunnel.py`

Changes to test files:
- `test_fs_helpers.py` and `test_commits_routes.py`: Added `auth.configure("")` to `_disable_auth` fixtures (auth no longer reads from `dashboard.DASHBOARD_PASS`).
- `test_dashboard.py` (in merlin-bot/): Changed `import dashboard as db` → `import merlin_app as db`, removed `_auth=None` from `api_health()` calls.
- `test_merlin.py` → `test_merlin_bot.py`: Updated all `@patch("merlin.` → `@patch("merlin_bot.`.

## Review Checklist

- [ ] `main.py` — Is the module registration correct? Any import order issues?
- [ ] `main.py` — Is `_patch_template_responses()` robust? Could it break with future FastAPI/Starlette updates?
- [ ] `main.py` — Is `_validate_config()` covering all necessary checks?
- [ ] `auth.py` — Is the `configure()` pattern clean? Any thread-safety concerns?
- [ ] `templates/base.html` — Dynamic sidebar renders correctly? Logout link present?
- [ ] `merlin_app.py` — Are all routes from `dashboard.py` preserved? Any missing endpoints?
- [ ] `restart.sh` — Are the kill patterns robust? Could they accidentally kill unrelated processes?
- [ ] `.env` split — Is anything missing from either file? Does `load_dotenv` order matter?
- [ ] `notes/routes.py` — `MEMORY_DIR` hardcodes `merlin-bot/memory` path. Is this correct?
- [ ] CWD integration — Are `set_cwd()` calls in `main.py` correct for all modules?
- [ ] Test coverage — Are all moved tests still testing the right thing?
- [ ] Docs — Do `CLAUDE.md`, `docs/*.md` accurately reflect the new architecture?
- [ ] No regressions — Run both test suites, take screenshots, verify live behavior.

## Commands

```bash
# Run root tests (core modules)
uv run --with pytest --with httpx --with croniter pytest tests/ --ignore=tests/test_touch_scroll.py -v

# Run bot tests
cd merlin-bot && .venv/bin/pytest tests/ -v

# Start the system
~/merlin/restart.sh

# Take screenshots (server must be running)
uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --user admin --pass merlin123

# Interactive screenshots with auth (use Playwright inline)
# See .claude/skills/screenshot/SKILL.md for the pattern

# View the full diff
git diff master...OS

# View diff for a specific file
git diff master...OS -- main.py
git diff master...OS -- merlin-bot/merlin_bot.py
```

## Key Files To Read (in order)

1. `main.py` — The new entry point (read fully)
2. `auth.py` — The `configure()` pattern (read the diff: `git diff master...OS -- auth.py`)
3. `merlin-bot/merlin_app.py` — The app plugin interface (read fully)
4. `templates/base.html` — Dynamic sidebar (read fully)
5. `restart.sh` — Process management (read fully)
6. `merlin-bot/merlin_bot.py` — Verify dead code was cleanly removed (`git diff master...OS -- merlin-bot/merlin_bot.py`)
7. `tests/conftest.py` + `merlin-bot/tests/conftest.py` — sys.path setup

## Known Issues / Edge Cases

1. **`_patch_template_responses()` monkey-patches `Jinja2Templates.TemplateResponse`** — This is fragile. If Starlette changes the signature, it breaks. Works today but worth flagging.
2. **`notes/routes.py` hardcodes `MEMORY_DIR = PROJECT_ROOT / "merlin-bot" / "memory"`** — Assumes merlin-bot is always present for notes to work. Could be made configurable.
3. **`restart.sh` kill pattern `pkill -f "uv run main.py"`** — Could theoretically match other processes with `main.py` in their args. Unlikely in practice.
4. **Two `conftest.py` files both manipulate `sys.path`** — If tests are run from wrong directory, imports could break.
