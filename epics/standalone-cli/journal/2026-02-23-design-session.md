# 2026-02-23 — Design Session: Standalone CLI

## Context

Merlin is currently repo-bound — you `cd merlin && uv run main.py`. We want it installable from anywhere like Claude Code: `curl | bash`, then `merlin` from any directory.

## Key Design Decisions

### Single folder (`~/.merlin/`)
- Considered XDG split (~/.config, ~/.local/share, ~/.local/state) — rejected as over-engineered
- Considered neovim-style (~/.config/nvim) — liked the simplicity
- Looked at how Claude Code does it: binary in `~/.local/share/claude/versions/`, user data in `~/.claude/`
- **Decision**: Everything in `~/.merlin/`. One folder to back up, one to nuke. But borrow Claude Code's version folder + symlink pattern for clean upgrades.

### Versioning: git tags only
- Considered VERSION file + git tags — rejected, they'll drift
- Considered VERSION file only with auto-tagging hook — unnecessary infrastructure
- **Decision**: Git tags are the single source of truth. `merlin version` reads the folder name from the `current` symlink. In dev mode, `git describe --tags`. Release = `git tag v0.3.0 && git push --tags`, nothing else.

### No CI/CD
- Python, no compilation needed. The tarball is the repo minus `.git/`.
- GitHub auto-generates tarballs for tags — no need to build anything.

### Installer philosophy
- Every system modification gets user confirmation (uv install, tmux install, PATH modification)
- uv is required (offer to auto-install), tmux/cloudflared are optional
- Detect package manager (apt/pacman/brew) for optional deps
- If user declines everything, still works — just prints manual commands

### Graceful degradation
- Missing tmux → terminal nav item grayed out (visible but not clickable), warning at boot with install command
- Missing cloudflared → tunnel disabled, warning at boot
- Merlin always starts — never crashes for missing optional deps

### Dev mode
- `merlin start --dev` runs from git checkout instead of `~/.merlin/current`
- User data still reads from `~/.merlin/` in both modes
- This is how you develop Merlin itself while having it installed

### User data separation
- memory/, cron-jobs/, data/, logs/ live at `~/.merlin/` root (not inside version folder)
- Upgrade = replace version folder + flip symlink. User data untouched.
- config.env at `~/.merlin/config.env` (not repo .env)

## What's NOT in the tasks yet
- How to handle the transition from current repo-based setup to installed setup (migration)
- Whether `restart.sh` / `merlin` shell alias needs updating
- Sub-app system for future (explicitly out of scope v1)
