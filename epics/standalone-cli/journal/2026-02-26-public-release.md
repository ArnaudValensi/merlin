# 2026-02-26 — Public Release Session

## What was done

### 1. Removed Discord channel ID from all tracked files
- Replaced hardcoded `1468668170599534655` across 21 files
- `merlin_bot.py`: default changed from the real ID to `""` (empty string — fail-fast validation catches missing config)
- Docs/examples: replaced with `YOUR_CHANNEL_ID` or `<channel_id>`
- Tests: replaced with fake ID `1234567890123456789`

### 2. Updated MERLIN_REPO default
- `cli.py` and `install.sh`: changed from `arnaud-music/merlin` to `ArnaudValensi/merlin`

### 3. Nuked git history and recreated fresh
- Deleted `.git/`, ran `git init`, added remote `https://github.com/ArnaudValensi/merlin.git`
- Single initial commit `bfa89e6` with all 195 tracked files
- Tagged `v0.1.0` on this commit
- Force-pushed to origin, deleted old `OS` branch from remote
- Added runtime data to `.gitignore`: `merlin-bot/logs/`, `merlin-bot/cron-jobs/.history.json`, `.locks/`, `.state/`
- Removed `epics/merlin-saas/` (user requested)

### 4. Pushed `~/.merlin/` to private repo
- Created `ArnaudValensi/merlin-data` (private) on GitHub
- Added remote, pushed all data (memory, cron-jobs, logs, config)
- Also stores `merlin-bot/personality.md` (bot personality, see below)

### 5. Split `merlin-bot/CLAUDE.md`
- **Problem**: The bot's CLAUDE.md contained personal personality config (name, background, communication style) that shouldn't be in a public repo
- **Solution**: Split into two files:
  - `merlin-bot/CLAUDE.md` (in public repo) — Technical directives only: how to use Discord skill, thread naming, memory system, message format, etc.
  - `~/.merlin/merlin-bot/personality.md` (in private data repo) — Personal personality: wizard persona, engineer background, communication style, bilingual preference
- **Injection**: `claude_wrapper.py` loads `~/.merlin/merlin-bot/personality.md` via new `_load_personality()` function, injected via `--append-system-prompt` alongside user memory. Same pattern as `_load_user_memory()`.
- Tests updated to mock `_load_personality` in the 3 tests that also mock `_load_user_memory`

### 6. Cleaned all personal information
- **Name "Arnaud"**: replaced with "Alex" in examples/tests, "the user" in journals. GitHub username `ArnaudValensi` kept (expected in repo URLs).
- **Location "Paris area, France"**: replaced with "San Francisco, CA" in docs/memory-system.md example
- **"Serveur de Arnaud"**: replaced with "My Server" in journal
- **"Merlin#0508"**: replaced with "Merlin#0000" in test data and journals
- **Yamaha RN1000A**: all references replaced with generic examples ("Check for new Python releases", "good mechanical keyboard", etc.) across cron skill docs, cron_manage help text, tests, kb_add examples, epic journals

### 7. Repo made public
- User made `ArnaudValensi/merlin` public on GitHub

## Current state

### Commit: `bfa89e6` — "Initial commit — Merlin v0.1.0"
- Single clean commit, no history
- Tag: `v0.1.0`
- Branch: `master`
- Remote: `https://github.com/ArnaudValensi/merlin.git` (public)

### Tests: 667 passing (314 core + 353 bot)

### Private data: `ArnaudValensi/merlin-data` (private)
- `~/.merlin/` with memory, cron-jobs, logs, config
- `merlin-bot/personality.md` (bot personality)

### Git credential helper
- Set via `git config --local credential.helper store` (was lost when `.git` was nuked)
- `~/.git-credentials` has the GitHub token

## Session 2 — Install flow testing & graceful degradation

### 8. Tested full install flow
- `curl -fsSL https://raw.githubusercontent.com/ArnaudValensi/merlin/master/install.sh | bash` — works
- Downloads v0.1.0 tarball, extracts to `~/.merlin/versions/0.1.0/`, creates `current` symlink and launcher
- `merlin version` → `0.1.0` ✓
- `merlin update` → `Already up to date (0.1.0)` ✓
- `merlin start --no-tunnel` → dashboard + bot + cron all start ✓
- User also verified manually from their terminal — all steps passed

### 9. Fixed: graceful degradation when Discord not configured
- **Problem**: `main.py` called `bot_plugin.validate()` which does `sys.exit(1)` if `DISCORD_BOT_TOKEN` or `DISCORD_CHANNEL_IDS` are missing. A fresh install with no Discord config would crash.
- **Fix**: `_validate_config()` now catches `SystemExit` from `bot_plugin.validate()` and calls `_disable_bot_plugin()` instead
- `_disable_bot_plugin()` sets `bot_plugin = None`, `show_bot_status = False`, resets nav to core-only
- Result: missing config errors are still printed (so users know what to set), but dashboard starts with core modules (Files, Terminal, Commits, Notes)
- Tests: 313 core + 353 bot all pass

## TODO for next session

### Must do:
1. **Commit, re-tag, and push** — The graceful degradation fix needs to go into the public repo. Amend the initial commit or create a new one, re-tag v0.1.0, force-push.

2. **Test update flow** — Create a v0.1.1 tag, verify `merlin update` picks it up

### Should verify:
- Dashboard + bot work after restart (personality loaded from `~/.merlin/merlin-bot/personality.md`)
- `restart.sh` still works
- Cron jobs still run (they read from `~/.merlin/cron-jobs/`)

### Nice to have:
- Add `config.env.example` to the public repo (template for `merlin setup`)
- Consider moving `epics/` to the private repo (they contain development history that new users don't need)
- Update `CLAUDE.md` to reflect the personality split and new file paths
