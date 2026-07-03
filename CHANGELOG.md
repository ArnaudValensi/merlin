# Changelog

All notable user-facing changes to Merlin are documented in this file.

## v0.22.0 (2026-07-03)

### Added
- **User manual** — Per-page manual under `docs/`, with the README rewritten as the front door: what Merlin is and how the pieces fit together.
- **Extension authoring guide** — `docs/creating-extensions.md` walks through building your own dashboard extension.
- **`merlin agent --personality` / `--user`** — Compose the agent's persona and user-context layers from the CLI.
- **Helpful CLI errors** — Missing required arguments now print the full command help instead of a terse error.
- **Seamless startup updates** — Accepting an update at interactive startup now re-executes the new version immediately, no manual restart.

### Fixed
- **macOS terminal freeze** — Disconnecting from the web terminal could deadlock the whole Merlin process inside the macOS kernel: unkillable process, dead dashboard, and a Merlin Cloud tunnel that stayed zombie until a hard restart. PTY I/O is now fully non-blocking, which makes the deadlock impossible; terminal cleanup also reliably reaps its tmux client.
- **macOS clipboard paste** — Cmd+V pastes text and images into the web terminal without a permission prompt, and Ctrl+V is no longer intercepted from terminal apps.

## v0.21.0 (2026-06-09)

### Added
- **`merlin skills`** — List every skill and where it comes from (core, an extension, or your personal skills), in precedence order. Shadowed skills and skills from disabled extensions are shown dimmed.
- **`merlin config skills-user-dir`** — Print the personal-skills home (`~/.merlin/skills-user/`).

### Changed
- **Operational skills always available** — The cron, notes, dashboard, and self-awareness skills now load whether or not the Discord bot is enabled. Previously they only activated with the bot on, so managed and bot-off environments had no operational skills.
- **Personal skills home** — Personal skills now live in `~/.merlin/skills-user/` (a dedicated home, separate from your notes). Per-environment.
- **Core skills can't be overridden** — A personal or extension skill that reuses a built-in skill's name is ignored, so a trusted core skill can never be silently shadowed (logged as a blocked override).

### Fixed
- **`merlin cron` shows help** — Running `merlin cron` with no subcommand now prints usage instead of an error.

## v0.20.1 (2026-06-08)

### Fixed
- **Colored `merlin --help`** — Help text renders with color again in Merlin Cloud / managed environments. Merlin now pins its Python interpreter to 3.14, so environments no longer resolve an older Python whose argparse help is uncolored.

## v0.20.0 (2026-06-08)

### Added
- **`merlin` subcommands** — Operate Merlin from the command line, from any directory: `merlin cron` (manage scheduled jobs), `merlin notes search` / `merlin kb add` / `merlin remember`, `merlin chat` (send/reply/react), `merlin dashboard-url`, and `merlin agent`. `merlin --help` is the catalog.
- **Extension commands** — Extensions can ship a `commands/` folder whose scripts appear automatically as `merlin <extension> <command>`.
- **Agent skills across engines** — Merlin's operational skills are exposed natively to Claude Code and OpenCode, and to your own terminal agents via `~/.claude/skills` and `~/.agents/skills`, so they work from any directory.
- **Skills & commands audit** — The Extensions page lists the skills and commands each extension ships, as a security-review surface.
- **`merlin-clip` for standalone installs** — The OSC 52 clipboard bridge (`pbcopy`/`pbpaste`) now ships with every install, not just managed containers.
- **Cron schedule builder** — Build schedules visually, with shell-command jobs (no agent, no token cost) and per-job timezones.
- **Cron performance tab** — A performance view with run/cost aggregation.
- **3D model preview** — Preview STL and OBJ files in the file browser.
- **Directory downloads** — Download a folder as a streamed zip, with progress feedback and a leave-page warning.

### Changed
- **Managed agents run where the job operates** — The Discord bot and cron agent jobs run in the configured working/launch directory, so an agent job pointed at a project repo picks up that repo's own context. Dev-only skills are no longer shipped to end users.
- **Web terminal uploads** — The upload button accepts any file type, not just images.
- **Cron page** — Notify control merges report-mode and the Discord toggle; expandable Logs rows; reordered tabs; accessibility and touch-target improvements.
- **Install & launcher** — `merlin` launches from a versioned `bin/merlin` that ships with each release (via `~/.merlin/current/bin`), so it never goes stale; `install.sh` gains `--non-interactive`.

### Fixed
- **Cron weekday selection** — Individual weekday chips in the schedule builder toggle correctly.
- **Mobile cron logs** — Expanded log rows no longer get inflated by mobile font-boosting.

## v0.19.0 (2026-05-05)

### Added
- **File viewer sibling navigation** — Prev/next buttons in the file viewer header to walk through siblings without bouncing back to the directory listing.

### Changed
- **Mobile file viewer header** — Header splits into two rows on narrow screens so the title stays readable.
- **Mobile sibling nav placement** — Prev/next controls dock to the right-thumb zone on mobile for one-handed reach.
- **File viewer accessibility** — Tighter touch targets and improved keyboard/reader semantics across the file viewer.

### Fixed
- **Mobile clipboard lock on Brave** — Copy from tmux/NeoVim no longer accumulates non-gesture clipboard API calls; touch devices route copy through the Copy pill instead, which preserves the gesture context Brave requires.

## v0.18.0 (2026-04-21)

### Added
- **Update indicator** — Current version shown in the sidebar footer; update to the latest release directly from the Settings page.

### Fixed
- **`merlin --saas-token TOKEN`** — The `--saas-token` flag now works without explicitly typing `start` (e.g. `merlin --saas-token mrl_...` is equivalent to `merlin start --saas-token mrl_...`).

## v0.17.0 (2026-04-08)

### Added
- **Notes sync** — Git-backed sync for your notes directory with automatic conflict handling, configurable debounce, and pull-on-startup.
- **`merlin config` command** — New CLI command to view and set configuration (e.g. `merlin config notes-dir`).
- **Git sync status** — Extensions settings page now shows live git sync status and a test connection button.

### Changed
- **Renamed "memory" to "notes"** — The memory system is now called "notes" throughout the UI, CLI, and configuration.

### Fixed
- **Clipboard paste** — Ctrl+V now correctly pastes images on desktop, and iOS text paste works reliably.
- **Mobile sidebar scroll** — Sidebar no longer inherits collapsed state from desktop, fixing scroll on mobile.
- **Notes sync conflicts** — Conflict markers are no longer pushed to remote; sync pulls on startup to stay current.
- **Route shadowing** — Sync-status API route no longer shadowed by catch-all path parameter.
- **Parse seconds crash** — Invalid duration input no longer crashes the debounce parser.
- **Notes extension UX** — Fixed layout and usability issues from UX review.

## v0.16.3 (2026-04-03)

### Added
- **Voice unavailable warning** — Mic button shows an amber warning badge when no transcription backend is configured. Clicking explains how to set up voice input.

### Fixed
- **macOS Intel install** — Merlin now installs on Intel Macs by skipping faster-whisper (no onnxruntime wheels for this platform). Voice input works via OpenAI API or Merlin Cloud.

## v0.16.2 (2026-04-03)

### Changed
- **Installer banner** — New block letter MERLIN wordmark with version number.
- **Simplified installer** — Removed unnecessary GitHub token authentication since the repo is now public.

## v0.16.1 (2026-04-03)

### Fixed
- **macOS installer** — Fixed package manager detection suggesting `apt` instead of `brew` on macOS.

## v0.16.0 (2026-04-03)

### Added
- **MIT license** — Merlin is now officially open-source under the MIT license.
- **Extension logging** — Extensions can now use `get_logger()` for structured logging with automatic context injection.

### Changed
- **New favicon** — Redesigned favicon with bordered Merlin logo.

### Fixed
- **Desktop clipboard** — Ctrl+V paste and Ctrl+Shift+C copy now work correctly in the web terminal.
- **Tmux copy** — Copy operations now pipe through merlin-clip for reliable clipboard sync.
- **NeoVim yy regression** — Clipboard sync only triggers on writeText failure, fixing the yank-line shortcut.
- **File browser buttons** — Fixed inconsistent button heights in the action bar.
- **Cron session viewer** — Fixed broken links to session transcripts.

## v0.15.1 (2026-03-28)

### Fixed
- **File browser selection mode** — Switched to icon-only buttons for visual consistency.

## v0.15.0 (2026-03-28)

### Added
- **File and directory download** — Download entire directories as zip archives from the Files app. New download button in the directory header and in selection mode for downloading multiple files or directories at once.

## v0.14.0 (2026-03-26)

### Changed
- **Environments rename** — "Projects" renamed to "Environments" across the sidebar switcher, API endpoints, and CLI help text to match the Merlin Cloud portal rename.

## v0.13.1 (2026-03-26)

### Fixed
- **Startup crash in installed mode** — Merlin failed to start after v0.13.0 update (`ModuleNotFoundError`) due to missing `--project` flag in the launcher script.

## v0.13.0 (2026-03-26)

### Added
- **Voice upload resilience** — Recordings are buffered in IndexedDB with automatic retry logic, so dropped uploads are recovered.

### Changed
- **Audio size limit** — Lowered from 100 MB to 25 MB to match the Groq transcription API limit.

### Fixed
- **Cron notifications** — Restored Discord threading and session continuity for cron job notifications.

## v0.12.0 (2026-03-23)

### Added
- **Voice input** — Server-side voice injection with upload progress indicator and auto-enter toggle.
- **Cron Logs tab** — Dedicated tab for viewing cron job execution logs with session viewer links.
- **Agent Engine** — Provider-agnostic execution layer. Switch between Claude Code and OpenCode from the Settings page.

### Changed
- **Cron modal** — Fullscreen on mobile with design system buttons and improved layout.
- **Discord channel settings** — Channel override hidden behind a link to reduce clutter.
- **Disabled cron jobs** — Card content is dimmed while controls remain interactive.

### Fixed
- **Auto-enter** — Now sends correct carriage return instead of newline.
- **Discord checkbox** — Fixed persistence and channel handling for old-format jobs.
- **Cron modal layout** — Fixed broken sticky header and padding issues on mobile.
- **Language selector** — Fixed vertical text centering.
- **Log viewer** — Fixed expanded row font size and bot table overflow on mobile.

## v0.11.0 (2026-03-19)

### Added
- **File management** — Create, rename, and delete files and folders from the file browser. Selection mode for batch operations, inline editing, and confirmation for destructive actions.

### Fixed
- **SSH terminal** — Fixed terminal rendering when client terminfo is missing by falling back to xterm-256color.

## v0.10.1 (2026-03-19)

### Fixed
- **SSH interactive sessions** — Shell and tmux sessions via ProxyJump now work. Previously, sessions closed immediately because the server used pipes instead of a PTY.

## v0.10.0 (2026-03-18)

### Added
- **Clipboard interop** — Full browser-to-container clipboard bridge with `merlin-clip` helper. Long-press paste pill (mobile), right-click paste (desktop), and image paste support.

### Changed
- **Long-press paste UX** — Shows a confirmation pill instead of reading the clipboard directly, improving privacy and user control.
- **Image upload limit** — Increased from 10MB to 25MB.

### Fixed
- **merlin-clip** — Fixed OSC 52 writes to go directly to tmux client TTY.
- **Clipboard permission icon** — Hidden on Safari and Firefox where the Clipboard API is not supported.

## v0.9.0 (2026-03-17)

### Added
- **Container SSH server** — SaaS mode containers now include an SSH server for direct container access.

## v0.8.1 (2026-03-17)

### Fixed
- **Restart reload timing** — `restart.sh` now polls for server readiness instead of using a fixed 2-second timeout.

## v0.8.0 (2026-03-16)

### Added
- **Extension system** — Three-tier extension architecture (core, built-in, installed) with a management page at `/extensions`. Toggle switches, config accordions, and tier badges.
- **Settings page** — Manage dashboard password and OpenAI API key from the web UI.
- **Bot monitoring tabs** — Three bot pages consolidated into a single tabbed page at `/bot`.

### Changed
- **Plugin → Extension terminology** — All UI and routes updated.
- **Accessibility improvements** — aria-labels and 44px touch targets on extension and settings pages.

### Fixed
- **Extension config security** — Config endpoint restricted to declared fields only.
- **Voice transcription** — Fixed module import when bot extension is disabled.
- **Session page navigation** — Back link corrected from `/logs` to `/bot/logs`.

## v0.7.0 (2026-03-16)

### Added
- **File upload** — Upload files via native file picker with progress bar. Multi-file upload, overwrite on conflict, camera/photos picker on mobile.
- **User plugin system** — Auto-discover and load plugins from `~/.merlin/plugins/`.

### Fixed
- **Header button spacing** — Balanced spacing above and below action buttons.

## v0.6.1 (2026-03-14)

### Added
- **Audio file preview** — Play audio files inline in the file browser (mp3, wav, ogg, m4a, flac, aac, webm, opus).

### Changed
- **Sidebar UX** — Sign-out moved to gear dropdown, projects in scrollable body with nav items.

## v0.6.0 (2026-03-14)

### Added
- **Project switcher** — SaaS mode sidebar shows all projects with online/offline status dots and quick switching.
- **Collapsible sidebar** — Collapses to an icon-only rail on desktop, state persisted in localStorage.

### Changed
- **Sidebar redesign** — Lucide SVG icons, compact nav on desktop, full touch targets on mobile, Merlin logo in header.
- **Unified brand green** — All accents standardized to `#4ade80` across the dashboard.

### Fixed
- **Sidebar collapse flash** — Fixed visual flash when navigating between pages with sidebar collapsed.

## v0.5.0 (2026-03-12)

### Added
- **Markdown & Mermaid rendering** — File browser renders markdown with syntax-highlighted code blocks and Mermaid diagrams.

### Changed
- **Vendored frontend dependencies** — All JS libraries, CSS, and fonts bundled as static files. Dashboard works fully offline.

### Fixed
- **Terminal status flash** — Fixed stale text briefly restoring on rapid status updates.

## v0.4.0 (2026-03-11)

### Added
- **Multi-repo commit browser** — Browse git history from any repository. Folder picker, repo indicator, and git submodule detection.
- **Cross-module navigation** — Git button in file browser, commits button in terminal toolbar.
- **File browser path memory** — Remembers last browsed path across visits.

### Changed
- **Folder picker** — Full-screen on mobile, resolves to git root, opens at current repo path.

### Fixed
- **Stale commits on repo switch** — Commits view now refreshes properly when switching repositories.
- **Terminal toolbar** — Fixed inconsistent button heights.

## Earlier releases (v0.1.0–v0.3.5, since 2026-01-31)

- **Standalone CLI** — Install via `curl | bash`, manage with `merlin start/version/setup/update`. Self-upgrade with daily update checks.
- **Web terminal** — Browser-based terminal via xterm.js + tmux with mobile key toolbar, voice input (Whisper API), and Nerd Font support.
- **File browser** — Filesystem browser with syntax highlighting, breadcrumb navigation, and clipboard image upload.
- **Commit browser** — Git commit viewer with full diff, syntax highlighting, tap-to-reveal deleted lines, and hunk navigation.
- **Notes editor** — Markdown editor with command palette, content search, CodeMirror with vim keybindings, image upload, and auto git commits.
- **Cloudflare Tunnel** — Public HTTPS access with cookie-based auth and login page.
- **SaaS tunnel** — SSH reverse tunnel for merlincloud.dev with dynamic port forwarding and portal auth bypass.
- **Voice transcription** — Three backends: local faster-whisper, OpenAI Whisper API, and SaaS proxy.
- **Bundled tmux config** — Dracula theme, tab management keybinds, NeoVim-friendly settings, OSC 52 clipboard.
- **Wizard hat favicon** — Custom favicon in SVG, ICO, and apple-touch-icon formats.
