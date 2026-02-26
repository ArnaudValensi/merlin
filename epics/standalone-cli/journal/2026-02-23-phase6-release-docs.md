# Phase 6: Release Process & Docs

**Date**: 2026-02-23
**Status**: Complete

## What was done

### Task 6.1: Document release process
- Created `docs/releasing.md` covering:
  - How to tag and publish releases
  - How installs/upgrades fetch from GitHub API
  - Version naming convention (semver, v prefix stripped for folder names)
  - Installed directory layout diagram
  - Local testing instructions (dry-run, mock upgrade)
  - Rollback procedure
  - Pre-release checklist

### Task 6.2: Update CLAUDE.md and architecture docs
- Updated `CLAUDE.md`:
  - Updated project description to mention `curl | bash` install
  - Added `cli.py`, `paths.py`, `install.sh` to project structure
  - Added `docs/releasing.md` and `docs/standalone-cli.md` to reference docs table
  - Added core component table entries for cli.py, paths.py, install.sh
  - Added CLI commands to development commands section
  - Added path resolution and graceful degradation to key patterns
- Updated `docs/architecture.md`:
  - Added path resolution diagram (dev vs installed mode)
  - Added startup flow diagram showing config validation and dep checking
- Created `docs/standalone-cli.md`:
  - Full design doc covering paths, directory layout, CLI subcommands, install script, upgrade mechanism, graceful degradation

### Task 6.3: End-to-end validation
- 302 core tests passing
- 353 bot tests passing (655 total)
- `cli.py version` prints correct version
- `install.sh --dry-run` runs all 9 steps correctly
- All docs match actual implementation

## Files changed

- `docs/releasing.md` (new)
- `docs/standalone-cli.md` (new)
- `docs/architecture.md` (updated — path resolution + startup flow diagrams)
- `CLAUDE.md` (updated — new files, commands, patterns)
- `epics/standalone-cli/tasks.md` (all tasks marked done)
