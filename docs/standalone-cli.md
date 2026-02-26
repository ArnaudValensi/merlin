# Merlin — Standalone CLI Design

## Overview

Merlin can run in two modes:

1. **Dev mode** (git checkout): `uv run main.py` — paths resolve from the repo root
2. **Installed mode** (`curl | bash`): `merlin start` — paths resolve under `~/.merlin/`

## Path Resolution (`paths.py`)

Central module that all other modules use for file/directory resolution.

### Dev Mode Detection

Priority order:
1. Explicit `set_dev_mode(True/False)` call (used by `cli.py --dev`)
2. `MERLIN_DEV=1` environment variable
3. `.git/` directory exists in the `paths.py` parent directory

### Path Functions

Only `app_dir()` differs between modes. User data always lives in `~/.merlin/`.

| Function | Dev Mode | Installed Mode |
|----------|----------|----------------|
| `app_dir()` | Repo root (where paths.py lives) | `~/.merlin/current/` |
| `data_dir()` | `~/.merlin/` | `~/.merlin/` |
| `config_path()` | `~/.merlin/config.env` | `~/.merlin/config.env` |
| `bot_config_path()` | `~/.merlin/config.env` | `~/.merlin/config.env` |
| `memory_dir()` | `~/.merlin/memory/` | `~/.merlin/memory/` |
| `cron_jobs_dir()` | `~/.merlin/cron-jobs/` | `~/.merlin/cron-jobs/` |
| `logs_dir()` | `~/.merlin/logs/` | `~/.merlin/logs/` |
| `merlin_home()` | `~/.merlin/` | `~/.merlin/` (or `MERLIN_HOME`) |

### Custom Install Location

Set `MERLIN_HOME=/custom/path` to override the default `~/.merlin/` location. All installed-mode paths resolve relative to this.

## Installed Directory Layout

```
~/.merlin/
├── bin/
│   └── merlin           # Launcher: exec uv run ~/.merlin/current/cli.py "$@"
├── versions/
│   ├── 0.1.0/           # Extracted release tarballs
│   └── 0.2.0/
├── current -> versions/0.2.0  # Symlink to active version
├── config.env           # User config (created by merlin setup)
├── memory/              # User data (survives updates)
├── cron-jobs/           # Scheduled jobs
├── logs/                # Logs
└── data/                # Session registry, structured log
```

## CLI Subcommands (`cli.py`)

| Command | Description |
|---------|-------------|
| `merlin` / `merlin start` | Start the dashboard (default subcommand) |
| `merlin start --port 8080` | Custom port |
| `merlin start --no-tunnel` | Disable Cloudflare tunnel |
| `merlin start --dev` | Force dev mode (resolve paths from repo) |
| `merlin version` | Print version |
| `merlin setup` | Interactive first-run wizard |
| `merlin update` | Download latest release, swap symlink |

### First-Run Setup

When `merlin start` runs and no `config.env` exists, it automatically triggers `merlin setup` which prompts for:
- Dashboard password
- Cloudflare tunnel (yes/no)
- Discord bot token (optional)

Results are written to `~/.merlin/config.env`.

## Install Script (`install.sh`)

9-step installer:
1. Print banner
2. Check/install uv (required)
3. Check/install tmux (optional)
4. Check/install cloudflared (optional)
5. Fetch latest release tag from GitHub API
6. Download and extract tarball to `~/.merlin/versions/<tag>/`
7. Create `~/.merlin/current` symlink (atomic: `ln -sfn` + `mv -Tf`)
8. Write `~/.merlin/bin/merlin` launcher script
9. Offer to add `~/.merlin/bin` to PATH
10. Create data directories

Supports `--dry-run` flag for testing.

## Update Mechanism

`merlin update`:
1. Read current version from `current` symlink target name
2. Fetch latest release tag from GitHub API
3. Compare — exit if already up to date
4. Download and extract new version
5. Atomic symlink swap (`ln -sfn` + `os.replace()`)
6. Print "Updated: v0.1.0 -> v0.2.0"

Old versions are kept for manual rollback: `ln -sfn ~/.merlin/versions/0.1.0 ~/.merlin/current`

## Graceful Degradation

At startup, `main.py` checks for optional dependencies:

| Dependency | If Missing |
|------------|------------|
| tmux | Terminal nav grayed out, `/terminal` returns 503, boot warning |
| cloudflared | Tunnel disabled, boot warning |

Package manager detection (apt/pacman/brew) provides correct install commands in warnings and UI tooltips.
