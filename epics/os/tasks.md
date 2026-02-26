# Epic: OS — Tasks

## Testing Strategy

Tests currently live in `merlin-bot/tests/` (20 files, pytest). After the restructure:

- **Core module tests** move to project root `tests/` (alongside the modules they test)
- **Merlin-bot tests** stay in `merlin-bot/tests/` (bot, cron, discord, memory, etc.)
- **Run tests after every phase** — no phase is done until tests pass

### Tests that move with their modules

| Test | Tests module | Action |
|------|-------------|--------|
| `test_auth.py` | `auth.py` | Move to root `tests/` |
| `test_fs_helpers.py` | `files/fs_helpers.py` | Move to root `tests/` |
| `test_terminal.py` | `terminal/routes.py` | Move to root `tests/` |
| `test_commits_routes.py` | `commits/routes.py` | Move to root `tests/` |
| `test_git_parser.py` | `commits/git_parser.py` | Move to root `tests/` |
| `test_touch_scroll.py` | `terminal/` (E2E) | Move to root `tests/` |

### Tests that stay in merlin-bot/tests/

| Test | Tests module | Notes |
|------|-------------|-------|
| `test_dashboard.py` | `dashboard.py` | Update: tunnel import path changes |
| `test_merlin.py` | `merlin.py` | Stays as-is |
| `test_claude_wrapper.py` | `claude_wrapper.py` | Stays as-is |
| `test_discord_send.py` | `discord_send.py` | Stays as-is |
| `test_cron_runner.py` | `cron_runner.py` | Update: tunnel import if needed |
| `test_cron_state.py` | `cron_state.py` | Stays as-is |
| `test_structured_log.py` | `structured_log.py` | Stays as-is |
| `test_session_registry.py` | `session_registry.py` | Stays as-is |
| `test_kb_add.py` | `kb_add.py` | Stays as-is |
| `test_memory_search.py` | `memory_search.py` | Stays as-is |
| `test_remember.py` | `remember.py` | Stays as-is |
| `test_memory_e2e.py` | Memory system E2E | Stays as-is |
| `test_transcribe.py` | `transcribe.py` | Stays as-is |

---

## Phase 1: Move core modules to project root

### Task 1.1: Move core infrastructure files
- **Status**: pending
- **Description**: Move `auth.py`, `tunnel.py` from `merlin-bot/` to project root. Update imports in any files that reference them. Leave copies or symlinks in `merlin-bot/` temporarily if `merlin.py`/`cron_runner.py` still need them.
- **Files**: `auth.py`, `tunnel.py`
- **Validation**: Files exist at root, imports resolve correctly

### Task 1.2: Move static files and templates
- **Status**: pending
- **Description**: Move `static/dashboard.css`, `static/dashboard.js` to `static/` at project root. Move `templates/base.html`, `templates/login.html` to `templates/` at project root. Keep Merlin-specific templates (overview, performance, logs, session) in `merlin-bot/templates/`.
- **Files**: `static/`, `templates/`
- **Validation**: Static files served correctly, templates render

### Task 1.3: Move core modules
- **Status**: pending
- **Depends on**: 1.2
- **Description**: Move `files/`, `terminal/`, `commits/`, `notes/` from `merlin-bot/` to project root. Update each module's path references (`MERLIN_BOT_DIR` → project root for `templates/base.html` resolution).
- **Files**: `files/`, `terminal/`, `commits/`, `notes/`
- **Validation**: Each module's routes load, templates render with base.html

### Task 1.4: Move and adapt tests for moved modules
- **Status**: pending
- **Depends on**: 1.1, 1.3
- **Description**: Create `tests/` at project root with `conftest.py`. Move `test_auth.py`, `test_fs_helpers.py`, `test_terminal.py`, `test_commits_routes.py`, `test_git_parser.py`, `test_touch_scroll.py` from `merlin-bot/tests/` to root `tests/`. Update import paths in each test. Update `merlin-bot/tests/test_dashboard.py` and `test_cron_runner.py` for changed tunnel import path.
- **Files**: `tests/conftest.py` (new), moved test files, updated bot test files
- **Test**: `cd /home/user/merlin && pytest tests/ -v` — all moved tests pass
- **Test**: `cd merlin-bot && pytest tests/ -v` — all remaining bot tests pass

## Phase 2: Create main.py entry point

### Task 2.1: Create main.py
- **Status**: pending
- **Depends on**: 1.1, 1.2, 1.3
- **Description**: Create `main.py` at project root. PEP 723 inline dependencies. Argparse with `--port`, `--host`, `--no-tunnel` flags. CWD = current directory at launch. Starts FastAPI app, registers auth routes (login/logout), mounts static files, registers core modules (files, terminal, commits, notes). Starts tunnel unless `--no-tunnel`. Reads `DASHBOARD_USER`, `DASHBOARD_PASS`, `TUNNEL_TOKEN` from `.env` at project root (or env vars).
- **Files**: `main.py`
- **Test**: `uv run main.py --no-tunnel` starts server, all core pages load at localhost:3123
- **Test**: Run root `pytest tests/ -v` — still passes

### Task 2.2: Make sidebar dynamic
- **Status**: pending
- **Depends on**: 1.2
- **Description**: Modify `base.html` to accept a template variable for nav items instead of hardcoding them. `main.py` passes the nav config (list of `{url, icon, label}`) to all template responses. Core items: Files, Terminal, Commits, Notes. App items added dynamically.
- **Files**: `templates/base.html`, `main.py`
- **Test**: Sidebar shows only core items when no app is loaded
- **Test**: All template-rendering tests still pass

## Phase 3: Merlin Bot as an app

### Task 3.1: Create merlin-bot app interface
- **Status**: pending
- **Depends on**: 2.1
- **Description**: Add an app entry point in `merlin-bot/` that exports a FastAPI router with the monitoring pages (overview, performance, logs, session) and their API endpoints (health, events, invocations, jobs). Also exports nav items metadata and static dir. Extract these routes from `dashboard.py` into a clean app module.
- **Files**: `merlin-bot/app.py` (new), refactor from `merlin-bot/dashboard.py`
- **Test**: `cd merlin-bot && pytest tests/ -v` — all bot tests pass (including test_dashboard.py adapted)

### Task 3.2: Register merlin-bot app in main.py
- **Status**: pending
- **Depends on**: 2.2, 3.1
- **Description**: In `main.py`, check if `merlin-bot/` exists and has the app interface. If so, import its router, register routes, add nav items to sidebar, mount its static files. Simple if-statement — no generic discovery yet.
- **Files**: `main.py`
- **Test**: When merlin-bot/ is present, sidebar shows Overview, Performance, Logs alongside core items
- **Test**: Both test suites pass: root `pytest tests/` and `cd merlin-bot && pytest tests/`

### Task 3.3: Update merlin.py to not start dashboard
- **Status**: pending
- **Depends on**: 3.2
- **Description**: Remove dashboard startup from `merlin.py` `on_ready()`. Bot only runs Discord listener + cron scheduler. Dashboard is now started by `main.py`. Update `restart.sh` to launch `main.py` instead (which starts the dashboard), and `merlin.py` separately (just the bot).
- **Files**: `merlin-bot/merlin.py`, `merlin-bot/restart.sh`
- **Test**: `cd merlin-bot && pytest tests/test_merlin.py -v` — passes with dashboard startup removed
- **Test**: Both test suites pass

## Phase 4: CWD integration

### Task 4.1: Pass CWD to modules
- **Status**: pending
- **Depends on**: 2.1
- **Description**: `main.py` resolves CWD at startup. Pass it to modules that need it: file browser (default browse path), commits (git repo path), terminal (starting directory), notes (notes data directory — `CWD/memory/` or similar).
- **Files**: `main.py`, module routes as needed
- **Test**: File browser opens at CWD, commits show CWD repo, terminal starts in CWD
- **Test**: Root `pytest tests/ -v` — all tests pass with CWD plumbing

## Phase 5: Cleanup and docs

### Task 5.1: Update documentation
- **Status**: pending
- **Depends on**: all above
- **Description**: Update `CLAUDE.md`, `docs/architecture.md`, `docs/dashboard-architecture.md` to reflect the new structure. Update project structure diagrams, file paths, startup instructions.
- **Files**: `CLAUDE.md`, `docs/*.md`
- **Validation**: Docs match reality

### Task 5.2: Verify everything works end-to-end
- **Status**: pending
- **Depends on**: all above
- **Description**: Full smoke test: launch `main.py`, verify all core pages, verify merlin-bot app pages, verify auth, take screenshots across viewports. Run both test suites one final time.
- **Test**: `pytest tests/ -v` — all root tests pass
- **Test**: `cd merlin-bot && pytest tests/ -v` — all bot tests pass
- **Test**: Screenshots across viewports — all pages render correctly
