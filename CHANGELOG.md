# Changelog

All notable user-facing changes to Merlin are documented in this file.

## v0.28.0 (2026-08-14)

### Added
- **Agent-state pills for Codex:** Codex windows now use the same `○` idle, `◐` working, `?` asking, and `●` done states as Claude Code in both the tmux status bar and the Sessions panel. Merlin installs marked lifecycle hooks in `~/.codex/hooks.json` alongside the existing Claude Code hooks, while preserving every user-owned entry.
- **A pill for "the agent is asking you something":** A window whose agent has stopped mid-turn to ask a question now shows a sky `?` in the tmux tab bar and the Sessions panel, instead of sitting on the amber `◐` as if it were still working. It covers the multiple-choice dialog, the plan-approval screen, and permission prompts. Unlike the green `●`, looking at the window does not clear it: only answering does.
- **Asking outranks waiting in the Sessions panel:** The Sessions button and the per-session badges turn blue when any window is blocked on a question, so a live turn waiting on you is distinguishable from a finished one you have not read yet.

### Changed
- **Context-aware new terminal windows:** New windows opened from the toolbar start in the active pane's directory, while windows opened from the Sessions panel start in the selected window's directory.

### Fixed
- **Renamed tmux session reconnects:** Browser and SSH terminals reconnect to renamed sessions instead of silently creating a duplicate default session.
- **Terminal environment after restart:** Restarting Merlin from tmux or a non-interactive shell no longer causes browser terminals to fail with "terminal does not support clear."
- **Agent-state consent banner on the Terminal page:** The banner stretched to fill the whole height of the terminal area instead of sitting as a strip at the top, squashing the terminal beside it. It now floats over the top of the page as intended. Other pages and mobile were never affected.

## v0.27.0 (2026-08-13)

### Added
- **Change directory from the terminal toolbar** — A folder button opens the picker, so you can `cd` by tapping through directories instead of typing the path.

### Fixed
- **Paste on iOS** — Pasting no longer reports "Clipboard blocked" when the clipboard plainly has text in it, and a paste that really does fail now says whether it was blocked, empty, or unavailable.

## v0.26.0 (2026-08-12)

### Added
- **Reorder windows by dragging** — Drag a window within its session in the Sessions panel to reorder it; the order is applied to tmux itself.

### Changed
- **Picking a window focuses the terminal** — Selecting a window in the Sessions panel moves the cursor straight into the terminal, so you can type without clicking in first.

### Fixed
- **Cleaner window reordering** — Dragging to reorder no longer selects the labels as text, and the window you picked stays selected afterward.

## v0.25.0 (2026-08-12)

### Added
- **Session switcher** — The web terminal's Sessions panel is now a full tmux session and window switcher. It shows every session and every window (not only ones running an agent), grouped as a tree, with the same ○ idle / ◐ busy / ● done activity dot on windows that carry an agent.
- **New sessions and windows from the panel** — A "new session" row at the top and a "new window" row inside each session, so you can spin up either without leaving the panel.
- **Rail mode** — Drag the panel narrow and it collapses to a column of dots. Hover a dot for a fly-out with the name and its rename/close actions; tap to switch.

### Changed
- **Per-client switching** — Tapping a session or window switches only the browser tab you are on; your phone and laptop stay where you left them. Selection is optimistic, so it feels instant.
- **Fold, rename, and close inline** — Click a session to fold it; rename or close any session or window from the panel. Sessions and windows keep tmux's own order and names.
- **Waiting reaches you across sessions** — The Sessions button badge counts windows finished and waiting on you across every session.

### Fixed
- **Panel activity stays in sync** — A window's dot now clears in the panel when you visit it, matching the tmux status bar even with a custom pill setup.

## v0.24.1 (2026-08-11)

### Fixed
- **No scrollbar on the web terminal** — The terminal no longer shows a scrollbar or reserves an empty gutter beside the Sessions panel; its cells fill the full width. Scrollback still works through tmux (mouse wheel and touch).

## v0.24.0 (2026-08-10)

### Added
- **Sessions board in the terminal** — A Sessions panel lives beside the web terminal (a docked, resizable panel on desktop, a thumb-first fullscreen sheet on mobile) listing your agent sessions with a filter, drag to reorder, and the current session called out unmistakably.
- **Knowledge base in agent context** — The KB index is injected into the agent's context, so the agent can draw on your notes.

### Changed
- **Keyboard toolbar shown by default** — The web terminal's secondary keyboard toolbar (Esc, Tab, Ctrl, Alt, arrows, paste, upload) is now visible by default on every device, not just touch screens. Toggle it off anytime.
- **Notes redesigned around the OKF format** — The knowledge base uses typed, OKF-style notes without auto-links. Notes docs and the notes skill were rewritten to match.

### Fixed
- **Voice auto-enter submits reliably** — Voice input now sends Enter as its own keystroke, so an auto-entered message actually submits.
- **Mobile Sessions sheet polish** — Fixed the first-open offset, kept the desktop button present when the panel is open, fixed the header buttons, and dropped the backdrop that greyed out the toolbar.

## v0.23.0 (2026-07-14)

### Added
- **Webhook triggers for jobs** — Trigger a job from outside Merlin with a secret-gated HTTP webhook, alongside or instead of a schedule. Create, rotate, and remove the secret from the job's Webhooks tab or the `merlin job` CLI.
- **Public URL in Settings** — Settings surfaces your instance's public URL (auto-discovered on Merlin Cloud) so webhook URLs and dashboard links are correct; you can override it.

### Changed
- **Cron is now "jobs"** — The cron system is renamed to jobs everywhere (dashboard, `merlin job` CLI, docs). A job's trigger — a schedule, a webhook, or both — is now an explicit, optional choice; a job with neither is manual-only.
- **Bundled Cloudflare tunnel removed** — Merlin no longer ships or manages cloudflared. Remote access is now bring-your-own tunnel/reverse proxy or Merlin Cloud.

### Fixed
- **"View session" always works** — The session transcript viewer is always available now, so "View session" links on the Jobs page no longer 404 when the Discord bot extension is disabled.
- **Empty schedule is rejected** — `merlin job add --schedule ""` errors instead of silently creating a trigger-less job.
- **Overlapping runs keep the schedule** — A manual or webhook run overlapping a scheduled time no longer causes that scheduled run to be skipped.
- **No dashboard freezes from job/settings paths** — Blocking work in the job-notification and settings-save paths is kept off the event loop, so it can't freeze the process.

## v0.22.1 (2026-07-03)

### Fixed
- **Changelog link in settings** — The update card's Changelog link opened an empty GitHub tag page; it now opens the changelog itself at the target version.

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

## Initial release (2026-04-17)

Feature summary at the first Merlin CLI commit:

- **Standalone CLI** — Install via `curl | bash`; manage with `merlin start/version/setup/update/config`. Self-upgrade with daily update checks and a sidebar update indicator.
- **Web terminal** — xterm.js + tmux with mobile key toolbar, voice input, and Nerd Font support. Container SSH server and ProxyJump access. Full clipboard interop via `merlin-clip` (OSC 52), including image paste.
- **File browser** — Syntax highlighting, breadcrumb nav, create/rename/delete with batch selection, any-file upload, file and directory download (zip), markdown & Mermaid rendering, and audio preview.
- **Commit browser** — Multi-repo git history with full diffs, folder picker, and submodule detection.
- **Notes editor** — Markdown editor with command palette, content search, CodeMirror vim keybindings, image upload, auto git commits, and git-backed sync.
- **Cron scheduler** — Built-in asyncio scheduler with job CRUD, REST API, logs tab, and Discord notifications.
- **Extension system** — Three tiers (core, built-in, installed) with Extensions and Settings pages.
- **Agent Engine** — Provider-agnostic execution: Claude Code (default) and OpenCode.
- **Voice transcription** — Local faster-whisper, OpenAI Whisper API, and SaaS proxy backends.
- **Discord bot** — Optional built-in extension with a monitoring page.
- **Tunnels** — Cloudflare Tunnel for public HTTPS (cookie auth), plus an SSH reverse tunnel for merlincloud.dev.
- **Open source** — MIT licensed.
