# 2026-02-25 — Continuation Notes

## What happened after the previous journal

### 7. Removed openclaw submodule
- Removed submodule, `.gitmodules`, and reference in CLAUDE.md
- Commits: `d086828`, `ab298d0`

### 8. Discord channel ID audit
- Found channel ID `YOUR_CHANNEL_ID` in 20 tracked files
- Files include: CLAUDE.md, merlin-bot/CLAUDE.md, merlin_bot.py, discord_send.py, cron_manage.py, .env.example, skills, docs, tests, epics
- User asked about it — not a security risk (channel IDs are public to server members) but reveals server structure
- **Decision pending**: remove or leave as-is

### 9. Merlin restarted
- `restart.sh` works correctly with the new paths (data from `~/.merlin/`)
- PID 99446

## Current state

### Branch: `OS` — latest commit `ab298d0`
```
ab298d0 Remove openclaw reference from CLAUDE.md
d086828 Remove openclaw submodule
96d80ee Add session journal for 2026-02-24: public release preparation
bed648f Clean up: remove local data copies and simplify .gitignore
ca7d532 Separate personal data from code repo for public release
ebb768f Add GITHUB_TOKEN support for private repo install and update
8e4fef6 Simplify release flow: use git tags directly, no GitHub Release needed
48ad2bf Rename 'upgrade' to 'update' and rewrite README for standalone install
```

### Key architecture (after data separation)
- `paths.py`: `app_dir()` = repo root (dev) or `~/.merlin/current/` (installed). Everything else always `~/.merlin/`.
- `~/.merlin/config.env` = single config file (dashboard creds + bot token + channel IDs)
- `~/.merlin/` has its own git repo (committed locally, no remote yet)
- Code repo HEAD is clean — no personal data, no secrets

### Tests: 667 passing (314 core + 353 bot)

### Tag: `v0.1.0` exists but is stale (pre-data-separation)

## TODO (prioritized)

### Must do before making repo public:
1. **Decide on Discord channel ID** — Remove from 20 files, or leave (not sensitive)
2. **Scrub git history** — Personal data in old commits. Options:
   - **Squash-merge OS → master** (simplest, loses commit history)
   - **git filter-repo** (keeps commits, rewrites history)
   - Accept it (data is just interests/gear/name, not secrets)
3. **Update `MERLIN_REPO` default** — `cli.py` and `install.sh` use `arnaud-music/merlin`, should match the actual public repo name
4. **Delete and re-tag `v0.1.0`** from master after merge
5. **Push `~/.merlin/` to private repo** — `ArnaudValensi/merlin-data` (private) on GitHub
6. **Make repo public** on GitHub

### Should verify:
- Dashboard + bot work end-to-end with data in `~/.merlin/` (restart.sh already confirmed working)
- `merlin update` works after repo is public (no GITHUB_TOKEN needed)
- `curl | bash` install works for a fresh user

### Nice to have:
- Add `config.env.example` to code repo
- Consider moving `merlin-bot/CLAUDE.md` (bot personality) to `~/.merlin/`
- Might want to move epics/archive/ content out too (contains Discord channel IDs, personal project details)
