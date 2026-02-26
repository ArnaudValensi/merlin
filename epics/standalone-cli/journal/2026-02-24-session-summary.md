# 2026-02-24 — Session Summary: Public Release Preparation

## What was done this session

### 1. Standalone CLI — Review Fixes (completed earlier, committed today)
- Two rounds of deep review (5 parallel agents each) found 24 issues
- All fixed: tarball security, config permissions, install.sh portability, version comparison, etc.
- Commit: `ca02172`

### 2. UI: Remove max-width constraints
- Removed `max-width: 960px` from `.commits-view` and `.files-view`
- Commits: `7add532`, `3938f13`

### 3. Rename `upgrade` → `update`
- CLI subcommand, function `run_update()`, test file `test_update.py`
- Updated all docs (CLAUDE.md, standalone-cli.md, releasing.md)
- Rewrote README — now leads with `curl | bash` install
- Commit: `48ad2bf`

### 4. Simplify release flow
- Removed GitHub Release requirement — just `git tag` + `git push --tags`
- `fetch_latest_tag()` now hits `/tags` API directly (no `/releases/latest` fallback)
- Same for `install.sh`
- Commit: `8e4fef6`

### 5. First release tag
- Created and pushed `v0.1.0` tag
- Tested full install flow with `GITHUB_TOKEN` (repo is private)
- Added `GITHUB_TOKEN` support to both `install.sh` and `cli.py` for private repos
- Verified: install, `merlin version` → `0.1.0`, `merlin update` → "Already up to date"
- Commit: `ebb768f`

### 6. Separate personal data from code repo
- Changed `paths.py`: user data always resolves under `~/.merlin/` regardless of dev/installed mode
- Only `app_dir()` differs between modes now
- Moved 71 personal files to `~/.merlin/` (memory, KB, cron-jobs, learning data, media, photos)
- Initialized private git repo at `~/.merlin/` with initial commit
- Created `~/.merlin/config.env` (merged from both .env files)
- Untracked and deleted personal data from code repo
- Cleaned up .gitignore
- Commits: `ca7d532`, `bed648f`

## Current state

### Branch: `OS` (ahead of master by ~40 commits)
All work is on the `OS` branch. Not yet merged to `master`.

### Tests: 667 passing (314 core + 353 bot)

### Tag: `v0.1.0` exists but points to pre-data-separation commit
Will need a new tag after merging to master.

### Personal data
- **Code repo**: Clean. No personal files tracked. No secrets.
- **`~/.merlin/`**: Private repo with all personal data. Committed locally, not pushed to any remote yet.
- **Git history**: Personal data still exists in old commits on the OS branch. If you want it fully scrubbed before making public, use `git filter-repo` or squash-merge to master.

## TODO for next session

### Before making repo public:
1. **Scrub git history** — Personal data (user.md, KB, photos, cron jobs) exists in old commits. Options:
   - Squash-merge OS into master (simplest — one clean commit, history lost)
   - Use `git filter-repo` to rewrite history (keeps individual commits, removes files)
   - Or accept it — the files are public-ish anyway (name, interests, etc.)

2. **Push `~/.merlin/` to a private repo** — Create `ArnaudValensi/merlin-data` (private) on GitHub and push

3. **Update `MERLIN_REPO` default** — Currently `arnaud-music/merlin`, should be `ArnaudValensi/merlin` (or whatever the public repo name will be)

4. **Re-tag after merge** — Delete `v0.1.0` and re-tag from master after merge

5. **Verify `restart.sh`** — Make sure the dashboard + bot still starts correctly with the new paths (data in `~/.merlin/`)

### Nice to have:
- Add `config.env.example` to the code repo (template for `merlin setup`)
- Consider whether `merlin-bot/CLAUDE.md` (bot personality) should stay in the public repo or move to `~/.merlin/`
- The default Discord channel ID was hardcoded in `merlin-bot/CLAUDE.md` — removed for public release
