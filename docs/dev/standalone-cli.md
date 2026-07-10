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
| `notes_dir()` | `~/.merlin/notes/` | `~/.merlin/notes/` |
| `jobs_dir()` | `~/.merlin/jobs/` | `~/.merlin/jobs/` |
| `logs_dir()` | `~/.merlin/logs/` | `~/.merlin/logs/` |
| `merlin_home()` | `~/.merlin/` | `~/.merlin/` (or `MERLIN_HOME`) |

### Custom Install Location

Set `MERLIN_HOME=/custom/path` to override the default `~/.merlin/` location. All installed-mode paths resolve relative to this.

## Installed Directory Layout

```
~/.merlin/
├── versions/
│   ├── 0.1.0/           # Extracted release tarballs
│   └── 0.2.0/
│       └── bin/merlin   # Launcher ships in the release; PATH points at current/bin
├── current -> versions/0.2.0  # Symlink to active version
├── config.env           # User config (created by merlin setup)
├── notes/               # User data (survives updates)
├── jobs/           # Scheduled jobs
├── logs/                # Logs
├── data/                # Session registry, structured log
├── extensions/          # Installed extensions (each may ship commands/ and skills/)
├── skills/              # Canonical aggregated skill dir (managed symlinks)
├── skills-user/         # Personal skills (per-environment, unsynced)
└── skills-plugin/       # Generated Claude Code plugin wrapping skills/
```

See [`skill-system.md`](skill-system.md) for how `skills/`, `skills-user/`, and `skills-plugin/` are built and surfaced to each engine.

## CLI Subcommands (`cli.py`)

| Command | Description |
|---------|-------------|
| `merlin` / `merlin start` | Start the dashboard (default subcommand) |
| `merlin start --port 8080` | Custom port |
| `merlin start --dev` | Force dev mode (resolve paths from repo) |
| `merlin version` | Print version |
| `merlin setup` | Interactive first-run wizard |
| `merlin update` | Download latest release, swap symlink |
| `merlin config` | List all resolved config values |
| `merlin config notes-dir` | Print the notes directory path |
| `merlin skills` | List every skill and its source (see [`skill-system.md`](skill-system.md)) |
| `merlin agent` | Print the agent-facing brain doc |
| `merlin job ...` | Manage scheduled jobs (wraps `job/manage.py`) |
| `merlin chat ...` | Discord transport: `send`/`reply`/`react`/`rename-thread` |
| `merlin dashboard-url` | Print the dashboard URL |

### First-Run Setup

When `merlin start` runs and no `config.env` exists, it automatically triggers `merlin setup` which prompts for:
- Dashboard password
- Discord bot token (optional)

Results are written to `~/.merlin/config.env`.

## Config Command

`merlin config` prints resolved configuration values. It reads from `config.env`, environment variables, and built-in defaults.

```bash
merlin config                        # List all values
merlin config notes-dir              # Print notes directory
merlin config home                   # Print Merlin home (~/.merlin)
cat "$(merlin config notes-dir)/kb/topic.md"  # Use in shell commands
```

Available keys: `notes-dir`, `skills-user-dir`, `home`, `app-dir`, `data-dir`, `config-path`, `logs-dir`, `sessions-dir`, `jobs-dir`, `extensions-dir`, `version`.

Read-only — use the Settings/Extensions UI or edit `config.env` directly to change values.

## Install Script (`install.sh`)

Installer steps:
1. Print banner
2. Check/install uv (required)
3. Check/install tmux (optional)
4. Fetch latest release tag from GitHub API
5. Download and extract tarball to `~/.merlin/versions/<tag>/`
6. Create `~/.merlin/current` symlink (atomic: `ln -sfn` + `mv -Tf`)
7. Launcher: none generated — `bin/merlin` (and `bin/merlin-clip`) ship in
   the release and are reached through `~/.merlin/current/bin`
8. Offer to add `~/.merlin/current/bin` to PATH
9. Create data directories

Supports `--dry-run` (preview) and `--non-interactive` / `-y` (no prompts;
required deps auto-install, optional deps are skipped, PATH added without
asking). The managed container reuses `install.sh --non-interactive` from
`merlin-setup.sh` instead of reimplementing the install.

The launcher ships in the repo at `bin/merlin` (`exec uv run --project
~/.merlin/current ~/.merlin/current/cli.py "$@"`, respecting `MERLIN_HOME`).
Because it lives at `current/bin`, it tracks the release and never goes
stale — there is no generated `~/.merlin/bin/merlin` to drift.

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

Package manager detection (apt/pacman/brew) provides correct install commands in warnings and UI tooltips.
