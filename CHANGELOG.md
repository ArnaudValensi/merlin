# Changelog

All notable user-facing changes to Merlin are documented in this file.

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
