# 2026-02-23 — Phase 2: CLI Entry Point

## Summary

Created `cli.py` at project root — the `merlin` command with subcommands `start`, `version`, `setup`, and `upgrade` (placeholder). Refactored `main.py` to expose `start_server()` so cli.py can delegate to it.

## What was done

### Task 2.1: CLI entry point
- `cli.py` with argparse subcommands: `start` (default), `version`, `setup`, `upgrade`
- `merlin` with no args = `merlin start`
- `start` accepts `--port`, `--host`, `--no-tunnel`, `--dev`
- `--dev` calls `paths.set_dev_mode(True)` before importing main
- Refactored `main.py`: extracted `start_server(port, host, no_tunnel)` from `main()`, keeping `main()` as backwards-compatible CLI wrapper

### Task 2.2: Version detection
- Dev mode: `git describe --tags --always`, strips `v` prefix, falls back to "dev"
- Installed mode: reads `current` symlink target folder name (e.g., `versions/0.3.0` -> `0.3.0`)
- Handles edge cases: git not found, timeout, no symlink

### Task 2.3: Setup wizard
- Interactive prompts for: dashboard password, tunnel enable (y/N), Discord bot token
- Writes to `config.env` (path from `paths.config_path()`)
- Shows current values when config exists, asks before overwriting
- Preserves unknown keys from existing config
- Creates parent directories if needed
- Auto-triggers on `merlin start` when no config exists (installed mode only)

### Task 2.4: CLI tests
- 31 tests in `tests/test_cli.py` covering:
  - Argument parsing (10 tests): all subcommands, flags, defaults
  - Version detection (8 tests): git describe, v-prefix stripping, symlink reading, fallbacks
  - Setup wizard (8 tests): config creation, tunnel, token, overwrite, extra keys, parent dirs
  - CLI routing (5 tests): version output, upgrade error, setup delegation, dev mode, arg forwarding

## Test results

- Core tests: 261 passed (230 Phase 1 + 31 new CLI tests)
- Bot tests: 353 passed (unchanged)
- Total: 614 passed, 0 failures
