# 2026-02-23 — Phase 4: Upgrade Mechanism

## Summary

Implemented `merlin upgrade` subcommand in cli.py. Fetches latest release from GitHub, compares versions, downloads tarball, extracts, and atomically swaps the `current` symlink.

## What was done

### Task 4.1: Upgrade subcommand
- `fetch_latest_tag()` — hits GitHub releases API, falls back to tags endpoint
- `download_and_extract(tag, target_dir)` — downloads tarball, strips top-level dir, extracts
- `atomic_symlink(target, link)` — creates temp symlink + `os.replace` for atomic swap
- `run_upgrade()` — orchestrates: read current version, fetch latest, compare, download if needed, swap symlink
- Keeps old versions in `versions/` for manual rollback (`ln -sfn`)

### Task 4.2: Upgrade tests
- 13 tests in `tests/test_upgrade.py`:
  - Atomic symlink: create, replace, old version preserved
  - Fetch latest tag: parse response, v-prefix stripping, network error handling
  - Upgrade flow: already up to date, new version, fetch failure, old version kept
  - Version detection: symlink reading, manual rollback scenario

## Test results

- Core tests: 289 passed
- Bot tests: 353 passed
- Total: 642 passed, 0 failures
