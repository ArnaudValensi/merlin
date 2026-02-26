# 2026-02-23 — Phase 1: Path Abstraction Layer

## Summary

Created `paths.py` at project root and migrated all modules to use it. This decouples every module from hardcoded repo-relative paths, establishing the foundation for installed vs dev mode path resolution.

## What was done

### Task 1.1: Created `paths.py`
- Central module with functions: `is_dev_mode()`, `set_dev_mode()`, `merlin_home()`, `app_dir()`, `data_dir()`, `config_path()`, `bot_config_path()`, `memory_dir()`, `cron_jobs_dir()`, `logs_dir()`
- Dev mode detected by: explicit override > `MERLIN_DEV` env var > `.git/` presence
- Custom install location via `MERLIN_HOME` env var
- Dev mode: app code from repo root, data from `merlin-bot/`
- Installed mode: app code from `~/.merlin/current/`, data from `~/.merlin/`

### Task 1.2: Migrated `main.py`
- `PROJECT_ROOT` now derives from `paths.app_dir()`
- Config loads from `paths.config_path()` and `paths.bot_config_path()`
- `_validate_config` uses `paths.config_path()` for error messages

### Task 1.3: Migrated merlin-bot scripts (12 files)
- **Import-only** (4): `claude_wrapper.py`, `cron_state.py`, `session_registry.py`, `structured_log.py` — added `import paths`, replaced path constants
- **Standalone** (6): `cron_runner.py`, `discord_send.py`, `memory_search.py`, `kb_add.py`, `remember.py`, `cron_manage.py` — added `sys.path.insert(0, ...)` for project root, then `import paths`
- **Plugin** (2): `merlin_bot.py`, `merlin_app.py` — imported paths, replaced constants
- Scripts that need CWD for subprocesses (`claude_wrapper.py`, `merlin_bot.py`) keep `_SCRIPT_DIR = Path(__file__).parent.resolve()` for that purpose only

### Task 1.4: Migrated notes module
- `notes/routes.py` replaced hardcoded `PROJECT_ROOT / "merlin-bot" / "memory"` with `paths.memory_dir()`
- Templates directory now resolves via `paths.app_dir() / "templates"`

### Task 1.5: Comprehensive tests
- 38 unit tests in `tests/test_paths.py` covering:
  - Dev mode detection (override, env var, git dir)
  - Dev mode paths (7 path functions)
  - Installed mode paths (8 path functions)
  - MERLIN_HOME override (4 scenarios)
  - First-run graceful behavior
  - set_dev_mode behavior
  - Module integration (6 modules verified)
  - Path absoluteness invariant
  - Dev mode directories exist on disk

## Test results

- Core tests: 230 passed (was 192, +38 new path tests)
- Bot tests: 353 passed (unchanged count, updated 1 test reference)
- Total: 583 passed, 0 failures

## Design decisions

1. **Functions over constants**: Path functions (not module-level constants) so tests can override via `set_dev_mode()` and env vars without import-time side effects.

2. **`_SCRIPT_DIR` pattern**: Scripts that need their own directory for subprocess CWD keep a local `_SCRIPT_DIR = Path(__file__).parent.resolve()`. This is separate from data path resolution.

3. **Standalone script bootstrap**: Scripts run via `uv run` add `sys.path.insert(0, str(Path(__file__).parent.parent))` before importing `paths`. This is one line of boilerplate per standalone script.

4. **`bot_config_path()` convergence**: In installed mode, `config_path()` and `bot_config_path()` return the same file (`~/.merlin/config.env`). In dev mode, they're separate (`.env` and `merlin-bot/.env`). This supports the single-config-file design for installed mode.
