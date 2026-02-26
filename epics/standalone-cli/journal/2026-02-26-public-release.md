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

## TODO for next session

### Must do:
1. **Test the full install flow** — The repo is now public. Test:
   - `curl -fsSL https://raw.githubusercontent.com/ArnaudValensi/merlin/master/install.sh | bash` (fresh install)
   - `merlin version` (should show 0.1.0)
   - `merlin update` (should say "already up to date")
   - Verify the installed version can start: `merlin start --no-tunnel`

2. **Test update flow** — Create a v0.1.1 tag, verify `merlin update` picks it up

### Should verify:
- Dashboard + bot work after restart (personality loaded from `~/.merlin/merlin-bot/personality.md`)
- `restart.sh` still works
- Cron jobs still run (they read from `~/.merlin/cron-jobs/`)

### Nice to have:
- Add `config.env.example` to the public repo (template for `merlin setup`)
- Consider moving `epics/` to the private repo (they contain development history that new users don't need)
- Update `CLAUDE.md` to reflect the personality split and new file paths
