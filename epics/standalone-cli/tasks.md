# Epic: Standalone CLI — Tasks

## Testing Strategy

Tests are written alongside each phase. Every task has explicit test criteria.

**Test locations:**
- `tests/` — Core module tests (existing) + new CLI/path tests
- `merlin-bot/tests/` — Bot-specific tests (existing, updated for new paths)
- `tests/test_installer.py` — Install script tests (new)
- `tests/test_cli.py` — CLI subcommand tests (new)

**Run after every phase:**
```bash
# Core tests
merlin-bot/.venv/bin/pytest tests/ --ignore=tests/test_touch_scroll.py -v

# Bot tests
cd merlin-bot && .venv/bin/pytest tests/ -v
```

---

## Phase 1: Path Abstraction Layer

Decouple all modules from hardcoded repo paths. This is the foundation — everything else depends on it.

### Task 1.1: Create path resolution module
- **Status**: done
- **Description**: Create a `paths.py` module at project root that centralizes all path resolution. It exposes functions/constants for: `app_dir()` (where code lives), `data_dir()` (user data — memory, cron-jobs, data), `config_path()` (config.env), `logs_dir()` (logs). In dev mode (detected by presence of `.git/` in app dir, or explicit flag), data_dir falls back to repo paths. In installed mode, everything resolves under `~/.merlin/`.
- **Files**: `paths.py` (new)
- **Test**: Unit tests for path resolution in both modes (installed vs dev). Test with `MERLIN_HOME` env var override for custom install location.

### Task 1.2: Migrate main.py to use paths module
- **Status**: done
- **Depends on**: 1.1
- **Description**: Replace all hardcoded `PROJECT_ROOT`, `MERLIN_BOT_DIR` path resolution in `main.py` with calls to `paths.py`. Config loads from `paths.config_path()`. Static/templates still resolve relative to app code dir.
- **Files**: `main.py`
- **Test**: `uv run main.py --no-tunnel` still starts correctly. Existing tests pass.

### Task 1.3: Migrate merlin-bot to use paths module
- **Status**: done
- **Depends on**: 1.1
- **Description**: Update merlin-bot scripts that reference `memory/`, `cron-jobs/`, `data/`, `logs/` to use paths module. Key files: `claude_wrapper.py` (memory path), `cron_runner.py` (cron-jobs path, logs), `merlin_bot.py` (data dir, logs), `discord_send.py` (session registry path), `memory_search.py`, `kb_add.py`, `remember.py` (memory paths), `cron_manage.py` (cron-jobs path), `cron_state.py` (state/history paths).
- **Files**: Multiple merlin-bot scripts
- **Test**: All bot tests pass. `cron_manage.py --list` still works. Memory scripts resolve correct paths.

### Task 1.4: Migrate notes module to use paths module
- **Status**: done
- **Depends on**: 1.1
- **Description**: Notes currently hardcodes `merlin-bot/memory/` as its data source. Update to use `paths.data_dir() / "memory"`. This is what makes notes work as user data that survives upgrades.
- **Files**: `notes/routes.py`
- **Test**: Notes module loads and saves from correct path. Existing notes tests pass.

### Task 1.5: Path abstraction tests
- **Status**: done
- **Depends on**: 1.1, 1.2, 1.3, 1.4
- **Description**: Comprehensive test suite for path resolution. Test: installed mode paths, dev mode paths, env var override (`MERLIN_HOME`), config.env loading from correct location, graceful behavior when `~/.merlin/` doesn't exist yet (first run).
- **Files**: `tests/test_paths.py` (new)
- **Test**: `pytest tests/test_paths.py -v` — all pass

---

## Phase 2: CLI Entry Point

Create the `merlin` command with subcommands.

### Task 2.1: Create CLI entry point script
- **Status**: done
- **Depends on**: 1.2
- **Description**: Create `cli.py` at project root. Uses argparse with subcommands: `start` (default — runs dashboard), `version`, `setup`, `upgrade`. `merlin` with no args = `merlin start`. `start` accepts `--port`, `--host`, `--no-tunnel`, `--dev`. `--dev` sets a flag that paths module uses to resolve from CWD repo instead of `~/.merlin/current`. Delegates to `main.py`'s startup logic (import, don't subprocess).
- **Files**: `cli.py` (new)
- **Test**: `uv run cli.py start --no-tunnel` starts dashboard. `uv run cli.py version` prints version. Subcommand routing works.

### Task 2.2: Implement `merlin version`
- **Status**: done
- **Depends on**: 2.1
- **Description**: In installed mode, resolve version from `current` symlink target folder name (e.g., `~/.merlin/current` → `versions/0.3.0` → version is `0.3.0`). In dev mode, use `git describe --tags` (fall back to "dev" if no tags). Print to stdout.
- **Files**: `cli.py`
- **Test**: Test version detection from symlink name. Test git describe fallback. Test "dev" fallback when no tags.

### Task 2.3: Implement `merlin setup` (first-run wizard)
- **Status**: done
- **Depends on**: 2.1, 1.1
- **Description**: Interactive setup that prompts for: dashboard password (with confirmation, allow empty for no auth), enable Cloudflare tunnel (y/N), Discord bot token (optional, skip with Enter). Writes results to `~/.merlin/config.env`. If `config.env` already exists, show current values and ask to overwrite. `merlin start` calls this automatically if `config.env` is missing.
- **Files**: `cli.py`, possibly `setup.py` (new) if logic is substantial
- **Test**: Test config.env generation with various input combinations. Test detection of missing config.env triggering setup. Test overwrite prompt when config exists.

### Task 2.4: CLI tests
- **Status**: done
- **Depends on**: 2.1, 2.2, 2.3
- **Description**: Test suite for CLI subcommands. Test argument parsing, subcommand routing, version detection, setup flow (mock stdin for interactive prompts).
- **Files**: `tests/test_cli.py` (new)
- **Test**: `pytest tests/test_cli.py -v` — all pass

---

## Phase 3: Install Script

The `curl | bash` installer.

### Task 3.1: Write install.sh
- **Status**: done
- **Depends on**: 2.1
- **Description**: Shell script that:
  1. Print banner ("Installing Merlin...")
  2. Check for uv — if missing, ask to install (`curl -LsSf https://astral.sh/uv/install.sh | sh`), require confirmation, fail if declined (uv is required)
  3. Check for tmux — if missing, detect package manager (apt/pacman/brew), offer to install with confirmation, skip if declined
  4. Check for cloudflared — same as tmux
  5. Fetch latest release tag from GitHub API (`https://api.github.com/repos/<owner>/<repo>/releases/latest`)
  6. Download tarball, extract to `~/.merlin/versions/<tag>/`
  7. Create `~/.merlin/current` symlink
  8. Write `~/.merlin/bin/merlin` launcher script (thin wrapper: `exec uv run ~/.merlin/current/cli.py "$@"`)
  9. Ask to add `~/.merlin/bin` to PATH in detected shell config (.bashrc/.zshrc) — if declined, print manual command
  10. Create `~/.merlin/` subdirs (memory, cron-jobs, data, logs) if they don't exist
  11. Print success message: "Run `merlin` to start"
- **Files**: `install.sh` (new, at repo root)
- **Test**: Dry-run mode (`--dry-run` flag) that prints what it would do without doing it. Test with mock GitHub API responses.

### Task 3.2: Write bin/merlin launcher
- **Status**: done
- **Depends on**: 3.1
- **Description**: The `~/.merlin/bin/merlin` script that the installer writes. It's a thin shell wrapper:
  ```bash
  #!/bin/bash
  exec uv run "$HOME/.merlin/current/cli.py" "$@"
  ```
  For `--dev` mode, `cli.py` handles path resolution internally — the launcher always points to the installed version.
- **Files**: Template in `install.sh` (written during install)
- **Test**: Launcher correctly invokes cli.py with all args forwarded.

### Task 3.3: Install script tests
- **Status**: done
- **Depends on**: 3.1
- **Description**: Test the install script with mocked system state. Test: uv detection and install prompt, package manager detection (apt vs pacman vs brew), tmux/cloudflared install prompts, GitHub API response parsing, tarball extraction, symlink creation, PATH modification prompt, dry-run mode output. Use a test harness that sources the script functions.
- **Files**: `tests/test_installer.sh` or `tests/test_installer.py` (new)
- **Test**: All install scenarios covered with mocks.

---

## Phase 4: Upgrade Mechanism

### Task 4.1: Implement `merlin upgrade`
- **Status**: done
- **Depends on**: 2.1, 3.1
- **Description**: The `upgrade` subcommand:
  1. Read current version from `current` symlink target folder name
  2. Fetch latest release tag from GitHub API
  3. Compare versions — if same, print "Already up to date (v0.3.0)" and exit
  4. Download tarball, extract to `~/.merlin/versions/<new_tag>/`
  5. Flip `current` symlink to new version (atomic: create temp symlink, then `mv -T`)
  6. Print "Upgraded: v0.2.0 → v0.3.0"
  7. Keep old versions in place (no auto-cleanup)
- **Files**: `cli.py` (upgrade subcommand logic, possibly `upgrade.py` if substantial)
- **Test**: Test version comparison. Test symlink flip (atomic). Test "already up to date" case. Test with mock GitHub API.

### Task 4.2: Upgrade tests
- **Status**: done
- **Depends on**: 4.1
- **Description**: Test upgrade flow end-to-end with a mock `~/.merlin/` directory. Test: version comparison logic, tarball download and extraction (mock HTTP), symlink atomic flip, rollback scenario (manually re-symlink to old version).
- **Files**: `tests/test_upgrade.py` (new)
- **Test**: `pytest tests/test_upgrade.py -v` — all pass

---

## Phase 5: Runtime Graceful Degradation

### Task 5.1: Grayed-out nav for missing deps
- **Status**: done
- **Depends on**: 1.2
- **Description**: At startup, `main.py` checks for tmux and cloudflared. If tmux is missing: terminal nav item gets a `disabled` flag + tooltip "tmux required — install: sudo apt install tmux" (with detected package manager command). The nav item renders grayed out in `base.html` (not clickable, reduced opacity, tooltip on hover). Terminal routes return a friendly "tmux not installed" page instead of crashing. Print warning at boot: `⚠ tmux not found — terminal disabled (install: <command>)`.
- **Files**: `main.py`, `templates/base.html`, `terminal/routes.py`
- **Test**: Test nav item disabled state rendering. Test terminal routes with tmux missing. Screenshot verification.

### Task 5.2: Boot warnings for missing deps
- **Status**: done
- **Depends on**: 5.1
- **Description**: At startup, detect missing optional deps and print clear warnings with the correct install command for the detected package manager:
  ```
  Merlin starting on http://0.0.0.0:3123
  ⚠ tmux not found — terminal disabled (install: sudo apt install tmux)
  ⚠ cloudflared not found — tunnel disabled (install: sudo apt install cloudflared)
  ```
  Package manager detection: check for `apt`, `pacman`, `brew` in that order.
- **Files**: `main.py`
- **Test**: Test warning output with various missing deps combinations. Test package manager detection.

### Task 5.3: Graceful degradation tests
- **Status**: done
- **Depends on**: 5.1, 5.2
- **Description**: Test that Merlin starts and serves all non-terminal pages when tmux is missing. Test that tunnel is skipped cleanly when cloudflared is missing. Test nav item rendering in disabled state.
- **Files**: `tests/test_graceful_degradation.py` (new)
- **Test**: `pytest tests/test_graceful_degradation.py -v` — all pass

---

## Phase 6: Release Process & Docs

### Task 6.1: Document release process
- **Status**: done
- **Depends on**: 4.1
- **Description**: Add `docs/releasing.md` documenting:
  - How to tag a release: `git tag v0.3.0 && git push --tags`
  - How GitHub Releases work (manual create from tag, attach nothing — tarball is auto-generated)
  - How the install script fetches releases
  - How to test the install script locally
  - How to do a manual rollback (`ln -sfn ~/.merlin/versions/0.2.0 ~/.merlin/current`)
- **Files**: `docs/releasing.md` (new)
- **Validation**: Instructions are accurate and complete

### Task 6.2: Update CLAUDE.md and architecture docs
- **Status**: done
- **Depends on**: all above
- **Description**: Update `CLAUDE.md` with: new directory structure (`~/.merlin/`), CLI subcommands, install instructions, dev mode usage, release process reference. Update `docs/architecture.md` with new path resolution flow. Add `docs/standalone-cli.md` covering the full design (folder layout, versioning, install, upgrade).
- **Files**: `CLAUDE.md`, `docs/architecture.md`, `docs/standalone-cli.md` (new)
- **Validation**: Docs match reality

### Task 6.3: End-to-end validation
- **Status**: done
- **Depends on**: all above
- **Description**: Full smoke test of the complete flow:
  1. Run `install.sh` (or simulate) — Merlin installs to `~/.merlin/`
  2. Run `merlin` — first-run wizard triggers, config saved
  3. Run `merlin` again — dashboard starts, all pages work
  4. Run `merlin version` — shows correct version
  5. Run `merlin setup` — wizard re-runs
  6. Run `merlin start --dev` — runs from git checkout
  7. Simulate `merlin upgrade` — new version extracted, symlink flipped, merlin starts with new version
  8. All test suites pass
  9. Screenshots across viewports — grayed nav items render correctly
- **Test**: All of the above
- **Test**: `pytest tests/ --ignore=tests/test_touch_scroll.py -v` — all pass
- **Test**: `cd merlin-bot && .venv/bin/pytest tests/ -v` — all pass
