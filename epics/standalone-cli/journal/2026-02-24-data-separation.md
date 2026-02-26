# 2026-02-24 — Separate Personal Data from Code Repo

## Context

Before making the repo public, we need to separate personal/user data from the code.
Currently in dev mode, `paths.py` resolves user data under `merlin-bot/` inside the repo.
This means 71 personal files are tracked in git (memory, cron-jobs, data, media, etc.).

## Goal

- Code repo = public, installable, no personal data
- User data lives in `~/.merlin/` (same as installed mode), tracked in a separate private repo
- Dev mode `paths.py` should read user data from `~/.merlin/` too (not from `merlin-bot/`)

## Plan

### 1. Update `paths.py` — dev mode data resolution
Change `data_dir()` to always return `merlin_home()` (~/.merlin/) regardless of mode.
Only `app_dir()` differs between dev/installed mode (repo root vs ~/.merlin/current/).

Before:
- Dev: `data_dir()` → `<repo>/merlin-bot/`
- Installed: `data_dir()` → `~/.merlin/`

After:
- Dev: `data_dir()` → `~/.merlin/`
- Installed: `data_dir()` → `~/.merlin/`

`config_path()` and `bot_config_path()` also need updating for dev mode.

### 2. Move personal files to ~/.merlin/
```
~/.merlin/
├── memory/          (from merlin-bot/memory/)
├── cron-jobs/       (from merlin-bot/cron-jobs/*.json)
├── data/            (from merlin-bot/data/)
├── logs/            (from merlin-bot/logs/)
├── .env             (bot config, from merlin-bot/.env)
└── config.env       (dashboard config, from .env)
```

### 3. Gitignore and untrack personal data
- Add `merlin-bot/memory/`, `merlin-bot/cron-jobs/*.json`, `merlin-bot/data/`, `merlin-bot/logs/` to .gitignore
- `git rm --cached` to untrack without deleting local files
- Keep `merlin-bot/cron-jobs/_example.json.template` tracked as example

### 4. Init private repo at ~/.merlin/
- `git init ~/.merlin/`
- Add and commit personal data
- Push to private GitHub repo

### 5. Update code references
- `merlin-bot/merlin_bot.py` — check if it hardcodes paths
- `merlin-bot/claude_wrapper.py` — same
- Any imports that assume data lives in merlin-bot/

### 6. Tests — update and run

## Files affected (71 tracked personal files)
- merlin-bot/memory/ — user.md, kb/ (34 files), logs/ (13 files), media/ (4 files), todos, histories
- merlin-bot/cron-jobs/ — 9 job files (keep template)
- merlin-bot/data/ — learning/piano/ (3 files), sample price data
- merlin-bot/CHANGELOG.md — personal changelog

## Status
- [ ] paths.py updated
- [ ] Files moved to ~/.merlin/
- [ ] Gitignore updated, files untracked
- [ ] Private repo initialized
- [ ] Code references updated
- [ ] Tests passing
- [ ] Committed and pushed
