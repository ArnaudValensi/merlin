# Changelog

All notable changes to Merlin are documented in this file.

## 2026-03-06

### Added
- **Clipboard image upload in web terminal** — Upload images via paste, drag-and-drop, or file picker. Images are saved to a temp directory (auto-cleaned after 1 hour) and the file path is injected into the terminal for use with CLI tools.
- **NeoVim-friendly tmux settings** — Bundled tmux config now includes escape-time, focus-events, true color, undercurl support, and increased history limit. User `~/.tmux.conf` is sourced if present.

### Fixed
- **Alt key handling in web terminal** — Fixed Alt/Option key on macOS producing composed characters instead of sending Alt sequences. Uses `macOptionIsMeta` and a custom key handler based on `e.code`.

## 2026-03-01

### Added
- **Python/FastAPI code review skill** — Senior engineer code review covering async correctness, FastAPI patterns, Pydantic v2, security (SSTI, injection, path traversal), PEP 723/uv conventions, and modern Python type hints.
- **Wizard hat favicon** — Custom favicon in SVG, ICO, and apple-touch-icon formats.

## 2026-02-28

### Added
- **OpenAI Whisper API transcription** — Voice transcription now supports three backends: OpenAI Whisper API (cloud, fast), SaaS proxy (stub), and local faster-whisper (default). Backend is auto-selected based on available API keys. Setup wizard and config updated accordingly.

## 2026-02-27

### Added
- **Bundled tmux config** — Web terminal now ships its own `tmux.conf` with Dracula theme, icon-based tab indicators (● ○), F2–F5 keybinds for tab management, mouse support, and OSC 52 clipboard integration.
- **Enter button on terminal toolbar** — Mobile toolbar now includes an Enter key that sends directly without triggering the virtual keyboard.

### Changed
- **Terminal toolbar icons** — Replaced F2/F3/F4/F5 text labels with visual icons (◀ ▶ + ✕) for tab navigation and management.
- **Epics moved to private repo** — Project planning and epic files moved to the private `merlin-saas` repository to keep the public repo focused on code.

### Fixed
- **Dev mode detection in submodules** — `paths.py` now uses `.exists()` instead of `.is_dir()` for `.git` detection, since git submodules use a `.git` file rather than a directory.

## 2026-02-26

### Added
- **Public release** — Repo made public at `ArnaudValensi/merlin`. Git history nuked, single clean initial commit, tagged v0.1.0.
- **Install flow verified** — Full `curl | bash` install tested: download, extract, symlink, launcher, `merlin version`, `merlin update`, `merlin start --no-tunnel` all working.
- **Startup update check** — `merlin start` now checks for new versions once per day and prompts to update before starting the server. Skipped in dev mode and rate-limited to avoid unnecessary API calls.

### Changed
- **Changelog moved to project root** — `merlin-bot/CHANGELOG.md` → `CHANGELOG.md`. Nightly cron job updated to match.

### Fixed
- **Graceful degradation without Discord** — Dashboard no longer crashes when `DISCORD_BOT_TOKEN` is missing. Bot plugin validation errors are logged as warnings and the bot is disabled, allowing the dashboard to start with core modules only (Files, Terminal, Commits, Notes).

## 2026-02-24

### Added
- **App ideas batch 21** — New project concepts: possession graph, spec sheet, client radar.
- **Private repo install** — `GITHUB_TOKEN` support in installer and update command for installing from private repositories.
- **Knowledge base entries** — Aseprite Normal Toolkit, self-improving coding agents.

### Changed
- **Public release preparation** — Separated personal data (memory, cron jobs, learning data, media) from the code repo into the `~/.merlin/` data directory. Paths module now resolves user data independently of install mode. Removed OpenClaw submodule reference.
- **Simplified release flow** — Uses git tags directly instead of GitHub Releases. Renamed `upgrade` CLI command to `update`. Rewrote README for standalone install.
- **Full-width dashboard views** — Removed max-width constraints from files and commits views for better use of screen space.

## 2026-02-23

### Added
- **Standalone CLI** — Merlin can now be installed via `curl | bash` and managed with CLI subcommands (`merlin start`, `version`, `setup`, `upgrade`). New `paths.py` abstracts dev mode (git checkout) vs installed mode (`~/.merlin/`). Includes `install.sh` installer, self-upgrade command, and graceful degradation for missing tmux/cloudflared (disabled nav items + 503 responses instead of crashes). 864+ lines of new tests across 4 test files.
- **Standalone CLI hardening** — 24 security and portability fixes: path traversal prevention in archive extraction, secure config file permissions (0600), atomic symlinks, portable `install.sh` (curl|bash and macOS/BSD compatible), and 13 additional tests.
- **App ideas batch 20** — New project concepts: KB Weaver, Voice Pipe, Pixel Font Forge.

### Changed
- **Cron jobs ephemeral by default** — Cron jobs now use ephemeral sessions to prevent cost growth from accumulating context. Cross-run knowledge uses the memory system (KB, logs) instead of session history.

## 2026-02-22

### Added
- **File browser** — Read-only filesystem browser in the dashboard with syntax highlighting, breadcrumb navigation, and mobile-first layout. Browse any directory on the host.
- **Major restructure** — Merlin is now a portable dev environment. Core modules (files, terminal, commits, notes) live at the project root. The bot is an optional plugin in `merlin-bot/`. New `main.py` entry point runs everything in a single process.
- **Single-process model** — Dashboard, bot, cron scheduler, and tunnel all run in one process via `main.py` instead of separate services. Bot exports a plugin interface (`router`, `start()`, `validate()`, `on_tunnel_url()`).
- **`restart.sh` script** — Quick restart alias (`merlin`) at project root for stopping and relaunching the whole system.

### Changed
- **Commit browser polish** — Diff mode toggle button (replacing per-line tap), hunk-level old-line reveal, FAB navigates between hunks, clickable hunk headers open file view, changed-line highlighting in file view, sticky file headers, edge-to-edge mobile layout.
- **Geist Mono font** — All monospace text across the dashboard now uses Geist Mono from Google Fonts.
- **Terminal-sized font** — Commit and file views use terminal-sized font with horizontal scroll for code readability.
- **Bot renamed** — `merlin.py` → `merlin_bot.py` to avoid confusion with root `main.py`.
- **Config split** — Dashboard `.env` at project root, bot `.env` stays in `merlin-bot/`.

### Fixed
- **Duplicate logs** — Fixed duplicate log output from bot process.
- **Cron runner crash** — Added missing `cwd=MERLIN_BOT_DIR` to subprocess call.
- **Tunnel URL notification** — Fixed Discord notification for tunnel URL with proper token argument and logging.
- **Stale cloudflared processes** — `restart.sh` now kills lingering cloudflared processes.
- **Diff view layout** — Shrunk line number columns, fixed block-level scroll.

## 2026-02-21

### Added
- **Commit browser** — Git commit browser in the dashboard with three views: commit list, commit detail with full diff, and file view with syntax highlighting. Mobile-first UI with tap-to-reveal deleted lines, inline diff annotations, and hunk navigation FAB. Full backend with git log/show/diff parsing. 658 lines of tests.
- **App ideas batch 19** — New project concepts: Voicing Drills, Crossover Calc, Token Burn.

## 2026-02-20

### Added
- **App ideas batch 18** — New project concepts: Frame Diff, Palette Lab, Groove Grid.
- **Knowledge base entries** — Rootless voicings (Bill Evans piano technique), Matt Greer 2D lighting tutorial, The Way to Jai (Balbaert's comprehensive guide).
- **Piano todo list** — Created a structured practice task list for piano learning.

### Fixed
- **KB gardening** — Added backlinks for 4 new notes, cross-linked water-reflection with LearnOpenGL, updated index.

## 2026-02-18

### Added
- **Terminal clipboard** — OSC 52 copy support captures terminal selections, with a floating copy pill that appears on selection and paste/copy buttons in the toolbar. Completes the terminal-clipboard epic.
- **App ideas batch 17** — New project concepts: Juice Knobs, Gear Ledger, Dungeon Sketcher.
- **Knowledge base entries** — Pixel-perfect outline shaders, LearnOpenGL gamma correction, Studio Dubroom 4 reggae rhythms (steppers, one-drop, two-drop, half-drop), one-drop reggae beat programming & fills.

### Fixed
- **KB gardening** — Fixed broken link, added backlinks for water-reflection and organ notes, updated reggae-piano summary.

## 2026-02-17

### Added
- **App ideas batch 16** — New project concepts: Riddim Box, Retro Profiler, Fin Dash.
- **Terminal clipboard epic** — Planned work for OSC 52 copy support and a paste button in the terminal (not yet implemented).
- **Knowledge base entries** — Water reflections for 2D pixel art (displacement shader technique), Berklee Bubblin' organ technique, Art of Reggae organ course.

## 2026-02-16

### Added
- **Cron reliability overhaul** — Per-job state files and locks replace the shared `.state.json`, staleness guard kills stuck jobs, and the dispatcher now runs jobs in parallel instead of sequentially. Extensive test coverage (698 new/changed lines of tests).
- **Reference documentation** — Comprehensive docs for all major subsystems: cron system, Discord bot, memory system, session management, auth & tunnel, web terminal, notes editor, session viewer, and dashboard architecture.
- **App ideas batches 14–15** — New project concepts: Room Tuner, Dotfile Forge, Devlog Reel, Particle Bench, Skill Tree, Tech Brief.
- **Knowledge base entries** — Stir It Up reggae piano progression with Aston Barrett bass line notes.

### Changed
- **Terminal touch handling** — Complete rewrite using a touch overlay with full SGR gesture support (tap, scroll, selection). 159 new test lines covering touch behavior.

### Fixed
- **Toolbar modifier keys** — Fixed Ctrl/Alt/Shift toolbar buttons sending duplicate characters.
- **KB gardening** — Added backlinks for dissolve-shader, cross-linked subpixel and dual-tilemap entries.

## 2026-02-15

### Added
- **Cloudflare Tunnel** — Dashboard and terminal now accessible over public HTTPS via Cloudflare Tunnel with zero port-forwarding. Cookie-based auth with login page, tunnel URL sent to Discord on startup with copy-to-clipboard button on the dashboard. Full test coverage (auth, tunnel, dashboard).
- **Language selector for voice transcription** — Terminal voice input now lets you pick the transcription language instead of defaulting to French.
- **App ideas batches 11–13** — New project concepts: Signal Flow, Grief Arc Mapper, Invoice Recon, Prompt Forge, Pocket Spotter, Scope Creep, Ear Gym, Jai Lens, Session Replay.
- **Knowledge base entries** — Subpixel animation technique.

### Changed
- **Terminal working directory** — Terminal sessions now start in `~/merlin` instead of `merlin-bot/` for broader project access.

### Fixed
- **Terminal bug fixes** — Fixed temp file leak in transcription endpoint, improved WebSocket exception handling (proper logging instead of silent swallowing), and added input validation for terminal resize commands.
- **Open redirect vulnerability** — Fixed potential open redirect in login flow.
- **Tunnel Discord notification** — Fixed missing token argument and logging in tunnel URL notification.
- **Mic API error message** — Clear error shown when microphone API is unavailable over HTTP (requires HTTPS).

## 2026-02-14

### Added
- **Web terminal** — Full browser-based terminal in the dashboard via xterm.js + WebSocket backend with PTY and tmux session persistence. Includes mobile key toolbar with sticky modifiers, voice input via microphone button (MediaRecorder + transcription API), and auto-reconnect with exponential backoff. 22 pytest tests covering the terminal module.
- **Thread renaming** — Merlin now renames Discord threads with descriptive titles on first message. New `[New thread]` flag triggers renaming, `rename-thread` command added to `discord_send.py`.
- **Long message threading** — `--thread-on-chunk` flag for `discord_send.py` creates a thread from the first chunk of a long channel message, keeping session continuity.
- **Shift+Tab button** — Mobile terminal toolbar now includes a Shift+Tab key for reverse-tabbing through completions.
- **App ideas batches 8–10** — New project concepts: fretboard atlas, print queue, merlin demo sandbox, tileset forge, inbox digest, time mosaic, soundscape forge, cut list optimizer, playtest recorder.
- **Knowledge base entries** — Dual tilemap autotiling technique, interconnexions données utilisateur.

### Changed
- **Terminal theme** — Switched from default dark theme to Dracula color scheme.
- **Terminal font size** — Reduced from 13px to 11px for more content density.
- **Mobile terminal layout** — Full-screen layout, locked iOS overscroll, menu integrated into status bar.
- **Touch scroll** — Mobile terminal scrolling now uses SGR mouse escape sequences for proper terminal scroll behavior.

### Fixed
- **Spurious error reactions** — Thread starter messages no longer get false `❌` reactions.
- **Touch scroll on mobile** — Fixed touch scrolling in terminal when iOS keyboard is open (capture-phase handler, removed `position:fixed` workaround).
- **Dead link in digest** — Fixed broken lalalapiano link in digest history.

## 2026-02-13

### Added
- **App ideas batch 7** — New project concepts: chord compass, van layout planner, sprite animator.
- **Knowledge base entries** — Blues piano progression in G (with major & minor blues scales), game thinking note with possession mechanic GIF.

### Changed
- **KB gardening report format** — Gardening cron job now outputs a numbered action list instead of prose sections, making findings easier to act on.

### Fixed
- **Nested session detection** — Stripped `CLAUDECODE` env var in `claude_wrapper.py` to prevent nested Claude invocations from incorrectly detecting an existing session.
- **KB gardening fixes** — Fixed summaries, duplicate headings, cross-links, and index completeness issues across several KB entries.

## 2026-02-12

### Added
- **App ideas batch 6** — New project concepts: lightmap debugger, dub delay designer, saas spy.

### Changed
- **KB gardening improvements** — Daily gardening cron job now checks index completeness (missing or stale entries in `_index.md`) and detects junk notes (body shorter than 50 chars).

## 2026-02-11

### Added
- **Content search** — Type `/` in the notes command palette to search inside note content. Results show matching lines with ±1 line of context, trimmed and centered around the match.
- **Fuzzy matching** — Content search upgraded from simple grep to fzf-powered fuzzy matching for more forgiving queries.
- **App ideas batch 5** — New project concepts: jig forge, tile weaver, mod tracker.
- **Knowledge base entries** — Merlin startup analysis & pitch, 2D visibility (Red Blob Games), Volcashare patch library, MAM MB-33 circuit mods, Tascam Model 12 tips, TB-303 beyond-acid techniques.
- **Playwright documentation** — Inline Playwright usage for interactive screenshots documented.

### Changed
- **KB gardening** — Cleaned up orphan notes, added bidirectional links, removed test entries, updated index.

### Fixed
- **Content search silent failure** — Search no longer fails silently when the API is unavailable.
- **Fuzzy match centering** — Trimmed content lines now properly center around the matched term.

## 2026-02-10

### Added
- **App ideas batch 4** — New project concepts: dialogue loom, acoustic box, context pilot.
- **Vim mode toggle for mobile** — Notes editor now has a toolbar button to toggle vim keybindings on/off, plus `jj` mapped to Escape for easier mobile editing.
- **Tag navigation** — Dedicated tag page in the notes editor. Click any tag to see all notes with that tag, with styled tag pills and responsive layout.
- **Piano learning content** — Blues 12 mesures en Do exercise added to KB. Piano student profile updated with initial assessment and multi-style goals.
- **Design system documentation** — Dashboard architecture doc now includes a formal design system section (colors, typography, spacing, components).

### Changed
- **Geist font** — Entire dashboard now uses Geist from Google Fonts for a cleaner, more consistent look.
- **Icon buttons** — Notes editor text buttons replaced with Lucide icon buttons, styled to match the dashboard design language.
- **Mobile notes layout** — Notes index header stacks vertically on small screens for better readability.

## 2026-02-09

### Added
- **Notes editor** — Full knowledge base editor in the dashboard. Browse, view, create, edit, and delete KB notes from the web UI. Includes image upload, markdown rendering, frontmatter parsing, and automatic git commits for every change.
- **Command palette** — Create new notes directly from the notes editor via a command palette.
- **CodeMirror editor** — Replaced the plain textarea with CodeMirror 5 featuring vim keybindings, syntax highlighting, and proper scrolling.
- **Cost tracking** — Dashboard now shows Claude API cost data on the overview and performance pages.
- **Ephemeral cron jobs** — Cron jobs can now be marked as ephemeral (auto-deleted after first successful run). Test/debug jobs cleaned up automatically.
- **App ideas batch 3** — New project concepts: Sokoban lab, MIDI patch bay, portfolio engine.

### Changed
- **Mobile navigation** — Replaced the floating hamburger menu with a static top bar for better usability on mobile.
- **Notes editor UX** — Full-page scrollable editing surface, consistent 15px text size across editor and viewer.

### Fixed
- **iOS auto-zoom** — Notes editor textarea no longer triggers Safari's auto-zoom on focus.
- **Cancel button state** — Edit mode cancel button now properly hides after canceling.
- **Media path resolution** — Images embedded in KB notes now resolve correctly in the viewer.

## 2026-02-08

### Added
- **Nightly changelog updates** — Automated cron job (3am daily) reviews git history and updates this changelog with user-facing summaries.
- **Daily self-reflection** — New cron job (7am daily) where Merlin writes a short journal entry reflecting on the previous day's work, observations, and open threads.
- **Teacher skill** — Adaptive learning coach for tracking progress and guiding practice sessions. Launched with a full piano learning module (pedagogy, session tracking, student profile).
- **Daily app ideas** — New cron job that brainstorms project ideas tailored to the user's skills and interests, with history tracking to avoid repeats.
- **Wake-up pings** — Lightweight cron jobs at 7:00, 12:05, and 17:10 to keep the bot process warm.
- **Knowledge base entries** — Reggae piano progressions, dissolve shader techniques, and multiple rounds of app ideas added to the KB.

### Changed
- **Cron schedule consolidation** — All nightly cron jobs rescheduled to the 2:00–3:00 window at 10-minute intervals, reducing overlap.
- **Daily digest dedup** — Digest now tracks previously shared news in a history file to avoid repeating stories.
- **Voice transcription UX** — Transcribed text and duration now displayed directly in the Discord thread before processing.

### Fixed
- **KB gardening** — Cross-links added between related notes, duplicate headings fixed, missing summaries filled in.

## 2026-02-07

### Added
- **Voice message transcription** — Merlin can now understand Discord voice messages. Audio is downloaded, transcribed locally with faster-whisper (medium model, French), and processed as a normal message. A microphone emoji shows during transcription, then swaps to the thinking emoji for Claude processing.
- **File attachments** — `discord_send.py` now supports `--file` flag for sending images, PDFs, and other files via Discord. Repeatable for multiple attachments.
- **Transcription dashboard logging** — Voice transcriptions appear in the structured log with duration, content, and author.

### Fixed
- **Cron re-dispatch race condition** — Long-running jobs (10-40 min) were re-dispatched every minute because `set_last_run()` was only called after completion. Now called before execution.

## 2026-02-06

### Changed
- **Built-in cron scheduler** — Replaced system crontab/cronie with an asyncio-based scheduler inside `merlin_bot.py`. Sleeps until :00 each minute, spawns `cron_runner.py` as subprocess. No external cron dependency.
- **Forced model** — All Claude invocations now use `claude-opus-4-6` via `DEFAULT_MODEL` in `claude_wrapper.py`.
- **Prefixed logging** — `[bot]` prefix for merlin, `[cron]` for scheduler. Dashboard request logging enabled.

### Added
- **Manual job execution** — `uv run cron_runner.py --job <job-id>` to run a specific job outside its schedule.
- **Git commit/push directive** — Bot instructions now require committing and pushing after making edits.

### Fixed
- **Dashboard and cron scheduler not starting** — `on_ready` handler in `merlin_bot.py` was named `on_ready_start_dashboard` which discord.py silently ignored. Renamed to `on_ready`.
- **Cron state files gitignored** — `.state.json` and `.history.json` are now properly excluded from git.

## 2026-02-05

### Added
- **Monitoring dashboard** — Web-based FastAPI dashboard on port 3123 with HTTP Basic Auth. Overview page (health cards + activity feed), Performance page (execution time charts), Logs page (filterable tabbed viewer), and Session viewer for inspecting Claude conversations.
- **Structured logging** — Single JSONL file (`logs/structured.jsonl`) as source of truth for the dashboard. Event types: `invocation`, `bot_event`, `cron_dispatch`.
- **Screenshot skill** — Multi-viewport screenshot utility for visual validation of web UI changes.
- **Test data generator** — `tools/generate_test_data.py` for dashboard development.
- **Dashboard architecture doc** — `docs/dashboard-architecture.md` for future UI work.
- **Dashboard link skill** — Merlin can share the dashboard URL on request.

### Changed
- **Resume-probe logging** — Failed resume probes no longer logged as invocation errors.
- **Message content in logs** — Structured logs now include message content and prompt.

## 2026-02-04

### Added
- **Discord thread sessions** — Every conversation lives in a Discord thread, mapped 1:1 to a Claude Code session. Channel messages create threads, thread messages resume sessions. Deterministic UUID5 from thread ID.
- **Self-awareness skill** — Merlin can introspect on his own architecture, source code, and runtime behavior.
- **Startup config validation** — All entry points validate required config at startup with descriptive error messages and setup instructions.

### Changed
- **Cron timezone** — Scheduler uses Europe/Paris timezone, configurable via `CRON_TIMEZONE` env var.
- **Cron max_turns** — Default changed to 0 (unlimited) for cron jobs.
- **Merlin personality rewrite** — Wizard, engineer, artist identity with bilingual communication style.

### Fixed
- **Prompt format** — Separate thread ID from channel ID in message context.
- **Session management** — Proper session creation on first call, resume after.

## 2026-02-03

### Added
- **Memory system** — Three-layer persistent memory: user facts (`memory/user.md`), daily logs (`memory/logs/`), and Zettelkasten knowledge base (`memory/kb/`).
- **Memory search** — `memory_search.py` for searching KB entries by keyword/tag and logs by keyword/date range.
- **KB management** — `kb_add.py` with automatic Zettelkasten link discovery between notes.
- **User memory** — `remember.py` for adding durable facts about the user.
- **PreCompact hook** — Extracts memories before context compression.

## 2026-02-02

### Added
- **Cron job system** — Job files in `cron-jobs/*.json`, dispatcher (`cron_runner.py`), management CLI (`cron_manage.py`), state/history tracking, and cron skill for Claude.
- **Job management CLI** — `cron_manage.py` for reliable job CRUD operations.
- **Self-documenting scripts** — All scripts have comprehensive `--help` with usage examples.

## 2026-02-01

### Added
- **Discord bot listener** — `merlin_bot.py` with `on_message` handler, reaction-based processing indicators, and persistent deterministic sessions.
- **Discord skill** — Send, reply, and react via REST API (`discord_send.py`).
- **Claude wrapper** — Single entry point for all Claude Code invocations with full logging (prompt, stdout/stderr, exit code, duration, usage stats).
- **Pre-tool hook** — Blocks force push and main/master branch deletion.
- **Epic-based project management** — Requirements, tasks, and journal entries for context restoration.

## 2026-01-31

### Added
- **Initial project setup** — CLAUDE.md project spec, OpenClaw submodule for architecture reference, two-file CLAUDE.md structure (project vs bot).
