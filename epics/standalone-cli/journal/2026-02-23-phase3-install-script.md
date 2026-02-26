# 2026-02-23 — Phase 3: Install Script

## Summary

Created `install.sh` — the `curl | bash` installer that sets up Merlin from scratch on any Linux machine. Includes dry-run mode for safe testing.

## What was done

### Task 3.1 + 3.2: install.sh and launcher
- 9-step installer: banner, uv check, tmux check, cloudflared check, GitHub release fetch, tarball extraction, symlink creation, launcher script, PATH setup, data directories
- Package manager detection: apt/pacman/brew
- uv is required (offer auto-install, fail if declined), tmux/cloudflared are optional
- All user-facing modifications require confirmation
- `--dry-run` flag prints everything it would do without making changes
- Launcher: `~/.merlin/bin/merlin` → thin bash wrapper that delegates to `uv run ~/.merlin/current/cli.py "$@"`
- Atomic symlink swap via `ln -sfn + mv -T`
- GitHub API: tries `/releases/latest`, falls back to `/tags`

### Task 3.3: Installer tests
- 15 tests in `tests/test_installer.py`
- Tests run `install.sh --dry-run` and verify output contains all expected steps
- Tests custom MERLIN_HOME env var override
- Tests package manager detection

## Test results

- Core tests: 276 passed (261 + 15 installer tests)
- Bot tests: 353 passed
- Total: 629 passed, 0 failures
