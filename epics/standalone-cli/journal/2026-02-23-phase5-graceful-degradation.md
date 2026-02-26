# Phase 5: Runtime Graceful Degradation

**Date**: 2026-02-23
**Status**: Complete

## What was done

### Task 5.1: Grayed-out nav for missing deps
- Added `_detect_pkg_manager()` to main.py — checks for apt, pacman, brew in order
- Added `_install_cmd(pkg)` — returns the correct install command for the detected package manager
- Added `_check_optional_deps(tunnel_enabled)` — checks tmux and cloudflared at startup
- Added `TMUX_AVAILABLE: bool = True` flag to main.py
- When tmux missing: sets `TMUX_AVAILABLE = False`, marks terminal nav item as `disabled` with tooltip showing install command
- When cloudflared missing: sets `TUNNEL_ENABLED = False` with warning
- Updated `base.html` to render disabled nav items as non-clickable `<span>` with reduced opacity and tooltip
- Added `.nav-disabled` CSS class to `dashboard.css`
- Updated `terminal/routes.py` to check `TMUX_AVAILABLE` and return 503 with helpful install instructions

### Task 5.2: Boot warnings for missing deps
- `_check_optional_deps()` logs warnings via the logger with the correct install command
- Warnings printed at startup with format: `tmux not found — terminal disabled (install: sudo apt install tmux)`
- Package manager detection supports apt, pacman, and brew

### Task 5.3: Graceful degradation tests
- Created `tests/test_graceful_degradation.py` with 13 tests covering:
  - Package manager detection (apt, pacman, brew, none)
  - Install command generation for each package manager + fallback
  - Nav item disabling when tmux is missing
  - Nav item stays enabled when tmux is present
  - Cloudflared missing disables tunnel
  - TMUX_AVAILABLE flag set correctly
  - Terminal route returns 503 with tmux info when TMUX_AVAILABLE is False

## Test results

- 302 core tests passing (13 new graceful degradation tests)
- 353 bot tests passing
- 655 total, all green

## Files changed

- `main.py` — Added `_detect_pkg_manager()`, `_install_cmd()`, `_check_optional_deps()`, `TMUX_AVAILABLE` flag
- `templates/base.html` — Conditional rendering for disabled nav items
- `static/dashboard.css` — `.nav-disabled` style
- `terminal/routes.py` — TMUX_AVAILABLE check with 503 response
- `tests/test_graceful_degradation.py` — 13 new tests

## Design decisions

- **Direct function test for terminal route**: Initially tried testing via FastAPI TestClient, but auth middleware caused redirect issues. Switched to testing `terminal_page()` directly with a MagicMock request, which is cleaner and tests the actual logic.
- **State restoration in tests**: All tests that modify main.py global state (TMUX_AVAILABLE, nav_items, TUNNEL_ENABLED) save and restore original values in try/finally blocks.
- **Package manager priority**: apt > pacman > brew, matching the order most likely for the target audience (Docker/Arch Linux first, macOS dev second).
