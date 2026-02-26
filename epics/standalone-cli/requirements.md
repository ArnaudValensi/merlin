# Epic: Standalone CLI — Install & Run Merlin From Anywhere

## Goal

Transform Merlin from a repo-bound `uv run main.py` project into a standalone CLI tool that can be installed with a single `curl | bash` command, run from any directory, upgraded in-place, and has a clean first-run experience.

## User Story

As a developer, I want to run `curl -fsSL <url> | bash` on any Linux machine to install Merlin, then type `merlin` in any directory to launch my dev environment there — and `merlin upgrade` to get the latest version.

## Acceptance Criteria

### Installation
- [ ] `curl -fsSL <url> | bash` installs Merlin
- [ ] Installer checks for uv, offers to auto-install if missing (with confirmation)
- [ ] Installer offers to install tmux and cloudflared via detected package manager (apt/pacman/brew) with confirmation
- [ ] Installer asks before modifying .bashrc/.zshrc for PATH; shows manual command if declined
- [ ] Installer scaffolds `~/.merlin/` directory structure
- [ ] Installer triggers first-run setup (or defers to first `merlin` launch)

### Directory Structure
- [ ] Single `~/.merlin/` folder contains everything
- [ ] Layout:
  ```
  ~/.merlin/
  ├── bin/merlin              # Launcher script (on PATH)
  ├── versions/
  │   ├── 0.1.0/             # Full app code (repo minus .git)
  │   └── 0.2.0/
  ├── current → versions/0.2.0  # Symlink to active version
  ├── config.env             # User config (tokens, passwords)
  ├── memory/                # Notes, logs, KB
  ├── cron-jobs/             # Cron job definitions
  ├── data/                  # Session registry, etc.
  └── logs/                  # All logs
  ```
- [ ] Upgrade only replaces `versions/<new>/` and flips `current` symlink — user data untouched
- [ ] `rm -rf ~/.merlin` is a clean uninstall

### Versioning
- [ ] Single source of truth: git tags (no VERSION file)
- [ ] Release process: `git tag v0.3.0 && git push --tags`
- [ ] No CI/CD required — Python, no build step
- [ ] `merlin version` reads version from `current` symlink target folder name
- [ ] In dev mode, `merlin version` uses `git describe --tags`
- [ ] Old versions kept in `versions/` for manual rollback

### CLI Subcommands
- [ ] `merlin` / `merlin start` — Start dashboard (CWD = browsed directory)
- [ ] `merlin start --dev` — Run from git checkout instead of `~/.merlin/current`
- [ ] `merlin upgrade` — Download latest release, extract, flip symlink
- [ ] `merlin version` — Print current version
- [ ] `merlin setup` — Re-run first-start interactive wizard

### First-Run Experience
- [ ] `merlin` with no `config.env` triggers interactive setup
- [ ] Prompts for: dashboard password, tunnel enable, Discord bot token (optional)
- [ ] Saves to `~/.merlin/config.env`
- [ ] `merlin setup` re-runs this anytime

### Runtime Behavior
- [ ] Merlin starts fine with missing optional deps (tmux, cloudflared)
- [ ] Missing tmux: terminal nav item grayed out with tooltip, warning at boot with install command
- [ ] Missing cloudflared: warning at boot with install command
- [ ] All modules read user data from `~/.merlin/` (memory, cron-jobs, data, logs)
- [ ] App code runs from `~/.merlin/current/` (or git checkout in dev mode)

### Path Refactoring
- [ ] All hardcoded paths in main.py and modules resolve user data from `~/.merlin/`
- [ ] Config loaded from `~/.merlin/config.env` (not repo `.env`)
- [ ] merlin-bot reads memory, cron-jobs, data, logs from `~/.merlin/` paths
- [ ] Dev mode (`--dev`) still works from repo checkout, using `~/.merlin/` for user data

### Tests
- [ ] Tests at every phase — no phase done until tests pass
- [ ] Install script tested (mock filesystem, dry-run mode)
- [ ] Path resolution tested (installed mode vs dev mode)
- [ ] Upgrade flow tested (symlink flip, version detection)
- [ ] First-run setup tested
- [ ] Grayed-out nav tested (missing deps)
- [ ] Existing test suites still pass after path refactoring

## Out of Scope (v1)
- Sub-app installation system (`merlin install <app>`) — future
- PyPI / pipx distribution — we use curl installer
- Windows / macOS support — Linux only for now
- Auto-update (checking on startup) — manual `merlin upgrade` only
- GitHub Actions release automation — manual tag + push for now
