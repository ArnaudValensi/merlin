# Web Terminal — Internals

Implementation reference for the browser-based terminal, `/terminal`: tmux
attached in control mode, its grid mirrored to the page as real DOM text in a
native scrolling container, and a real text field for the keyboard. The user
guide is [`docs/terminal.md`](../terminal.md); this doc covers how it is built.
It was built by the native-terminal epic (`epics/cli/native-terminal/` in
merlin-saas, journal included) beside the previous xterm.js terminal, which
the legacy-terminal-removal epic then removed (`epics/cli/legacy-terminal-removal/`).

## Architecture

```
Browser: real DOM text in a native scroll container, one real text field
  ↕ WebSocket (/ws/terminal2): binary grid frames, \x00 + JSON control frames
terminal/native.py: one tmux control-mode client per page (tmux -C, pipes)
  ↕
tmux 3.8 or newer (required at launch; checked again by the bridge)
  ↕
zsh shell
```

## Requirements: tmux 3.8

Merlin refuses to start without tmux 3.8 or newer, in `main._check_tmux`,
run by `_validate_config()` beside `_check_fd()` before anything starts:
exit status 1, the reason on stderr and in `merlin.log`. The minimum is
`native.MIN_TMUX` and the version is read with `native.parse_tmux_version`
(one definition of each; a build without a number such as `master` passes).
Two things are read, each bounded by `TMUX_PROBE_TIMEOUT` and with `$TMUX`
withheld so the default server is meant, as the bridge does: the binary with
`tmux -V` (no answer or a failure reads as unknown and refuses), then the
server with `tmux list-sessions -F '#{version}'`, which creates, attaches and
kills nothing. Its failure is classified from what tmux says: "no server
running on" (a stale socket) or "error connecting to ... (No such file or
directory)" (no socket) is no server, which is fine (Merlin starts one later);
any other failure or no answer is a server Merlin cannot vouch for (a protocol
mismatch after an upgrade, a socket tmux cannot open) and refuses, since the
bridge would talk to it. An upgraded binary does not replace a server started
by an older one, hence the second read. The messages name what was found and
the way out (`tmux is not installed`, `tmux 3.4 is too old`, `A tmux 3.4
server is still running`, `The tmux server could not be checked`, each
followed by `Merlin needs tmux 3.8 or newer.`); `docs/getting-started.md`
("tmux 3.8 or newer") has the troubleshooting entry for each. Only the
server's start is gated: a CLI command that does not start the server never
runs the check. `merlin restart` watches the relaunched process (uv, then the
Python it starts, which is what writes the state file) until that state
appears; when the process exits first, restart prints the last lines of
`nohup.out`, where the refusal is. Pinned by `tests/unit/test_startup_checks.py`
with a fake `tmux` first on PATH (missing, 3.4, 3.7c, 3.8, next-3.9, master, a
binary that hangs or fails, a running server older than the binary, a server
probe that fails or hangs): no test reaches the real default server.
`install.sh` treats tmux as required and warns with the same advice when
`tmux -V` is older than 3.8.

**At run time**, a server can still be replaced by an older one while Merlin
runs. The bridge checks the server it attaches to (below) and, when it is too
old, sends the page an `unsupported` control frame and closes the socket with
`CLOSE_UNSUPPORTED` (4426). The page shows it as its notice over the terminal
(`#nt-notice`, `tmux 3.4 is too old. Merlin needs tmux 3.8 or newer.`), in
the status bar, and does not reconnect from that close.

## Frontend

### The page

`terminal/templates/terminal-native.html`: the terminal (`#nt-container`),
the jump-to-bottom button, the notice line, the Copy pill, the input field
(`#nt-input`), the toolbar, and the chrome it shares with nothing else any
more but which stays in partials because the diagnostic page and the voice
console reuse pieces of it: `terminal-status-bar.html` (the bottom bar) and
its rules `terminal-status-bar.css`, `terminal-panels.html` (Sessions panel,
Apps panel, notifications popover, voice console, folder picker),
`terminal-head.html` (their stylesheets and early scripts) and
`terminal-chrome.html` (board, notifications, Apps, the clipboard ladder, the
session identity, the Sessions panel wiring). A narrow-screen rule of the bar
(the status text gives way with an ellipsis, the buttons tighten below
420 px) keeps every button on a phone's screen. The page's scripts are in
`terminal/static/native/` (below); the vendored xterm.js (`xterm.min.js`,
never opened) and its Unicode 11 addon are loaded for the decoder's parser.

### Connection

```javascript
const attempted = MerlinSessionIdentity.read();
engine.connect();  // wsUrl: MerlinSessionIdentity.websocketUrl(location, attempted)
```

- `session-identity.js` builds the `/ws/terminal2` URL with the stored
  identity (`tests/js/session-identity.test.js` pins it).
- Auto-reconnect with exponential backoff (1s → 30s max)
- Status indicator: green dot (connected), yellow (connecting), red (disconnected)
- Auth failure (close code 4401) stops reconnection; so does the `unsupported`
  close (4426), see above.
- The last server-confirmed tmux identity is stored in `sessionStorage`, which
  keeps browser tabs independent and survives a refresh in the same tab.
- A `session` control frame replaces the stored identity after initial attach
  and after every session switch.
- A connection that closes before confirming its attempted identity normally
  removes that identity before retrying. Close code 1013 means the tmux sweep
  was temporarily unavailable, so the confirmed identity is preserved.
- Cleanup is conditional, so an older socket cannot erase a newer confirmation.

### Addresses

`/terminal` renders the page; the manifest's `start_url` is `/terminal`.
`/terminal/native`, the page's address while the previous terminal existed,
answers a 307 redirect to `/terminal` that keeps the query (a deep link's
`?target=`), for bookmarks and pages opened before the change. A
`?terminal=` parameter, which once chose between two terminals, is ignored;
the settings page has no terminal section, and `static/settings.js` removes
the stale `merlin-terminal` key from `localStorage` on load (inside
try/catch). Pinned by `tests/unit/test_terminal_pages.py` and the addresses
tests of `tests/e2e/test_terminal_native_parity.py`.

### Installed App Bottom Bar

Installed on a phone (the manifest's `standalone` display), the page runs under the
home indicator strip and the screen's rounded corners, so `#terminal-status` gains a
34pt bottom padding under `@media (display-mode: standalone)`. The sessions sheet and
the notifications popover stack on that bar and follow it. While the software
keyboard is open the bar sits on the keyboard instead, so `followKeyboard()` marks
the document with `keyboard-open` and the padding drops. Nothing changes in a
browser tab.

`viewport-fit=cover` with `env(safe-area-inset-*)` was tried first and reverted: it
changes how iOS scrolls the page when the software keyboard opens, and the page had
no handling of its own then. Playwright cannot emulate the insets, so verify on a
phone screenshot.

### Software Keyboard

The keyboard slides over the page without resizing it, and iOS scrolls a focused
field into view only when a typed word is committed (a space, punctuation, return
with predictive text on), so without help the input line stayed behind the keyboard
until the first space. `followKeyboard()` in `page.js` sizes `.main` to
`window.visualViewport.height` whenever it is smaller than the layout viewport and
pins the scroll at the top, so the bottom bar and the input line land above the
keyboard as it animates. It runs only on coarse-pointer devices and leaves a
pinch-zoomed page alone. A backgrounded app loses its keyboard while it is
suspended, so the closing resize never reaches the page: back in front, the column
kept the keyboard height, and the terminal kept its focus with no keyboard showing
until a tap. The page blurs the focused field when it is hidden and measures
again on `visibilitychange`, `pageshow` and window `focus` (at once, next frame,
300 ms later, as the viewport settles). The window keeps its height while the
keyboard is up (see `fit` in `engine.js`), so the program is not redrawn each
time the keyboard moves. Playwright cannot open the iOS keyboard: verify on the
phone, keyboard opening and closing, in a Safari tab and in the installed app
(`tests/e2e/test_terminal_keyboard.py` drives a fake visual viewport for the
backgrounded case).

### Clipboard Plumbing

A program's copy goes through `merlin-clip`, which loads the text into the
`merlin-clip` tmux paste buffer (and writes OSC 52 to a tty client, if one is
attached); the bridge watches `%paste-buffer-changed` and sends the page a
`clipboard` frame. The user's own copy is the platform's: a selection over
real text. All paste paths use one unified handler, supporting text and images.
Images upload to `/tmp/merlin-clipboard/` and the path is typed into the
terminal.

The read itself lives in `terminal/templates/clipboard-core.js` — a Jinja partial included by `terminal-chrome.html` and `clipboard-test.html`, so the diagnostic page always runs exactly what the terminal runs. It tries `clipboard.read()` (the only API that returns images), then falls back to `clipboard.readText()` whenever `read()` came back with nothing pasteable. Do not add a second copy of that ladder anywhere: `tests/unit/test_clipboard_core.py` fails if you do, and the last time the logic existed twice the diagnostic page reported success on the path where the terminal was broken. Behaviour is covered by `tests/js/clipboard-core.test.js` (per-browser cases) and `tests/e2e/test_terminal_paste.py` (real browser).

**tmux configuration** (`terminal/tmux.conf`, NOT `~/.tmux.conf`): the web terminal starts tmux with `terminal/tmux.conf`, which loads first and sources `~/.tmux.conf` at the end. tmux's own mouse copy (a tty client's) is handled by `copy-pipe-and-cancel "merlin-clip copy"` — copy text and send it to the browser clipboard in one step.

User-facing copy/paste recipes (shortcuts, pills, NeoVim config, `/terminal/clipboard-test`) live in [`docs/terminal.md`](../terminal.md); the whole clipboard design is in `docs/merlin-cli/clipboard.md` of merlin-saas.

## Backend

### WebSocket Endpoint (`/ws/terminal2`)

`native_ws` in `terminal/native.py`, wired through `register_routes(app)` in
`terminal/routes.py` (the escape hatch: a WebSocket upgrade cannot use the
HTTP auth dependency):

1. Verify auth via session cookie; a refused login is accepted then closed
   with 4401 (a socket closed before its handshake is answered reads as a
   dropped network in a browser, which would reconnect for ever)
2. Sweep the live tmux sessions and validate the optional browser identity
3. On a transient sweep failure, close with code 1013 before any attach so
   the browser retries without discarding its identity
4. With no session (no server), read the installed binary's version: too old
   is refused with the `unsupported` frame and `CLOSE_UNSUPPORTED`
5. Attach a control client (`NativeBridge.start`): the attach ladder below in
   control mode, the environment of "What a terminal inherits"; the server
   it reached is asked for its version and the formats the bridge needs, and
   refused the same way when too old
6. Run the bridge: updates of the displayed pane, history on request, input,
   paste, keys, resize, the `session` and `windows` frames, the clipboard
   frames, the control messages (`switch`, `visibility`, `ping`,
   `clipboard_sync`, `active`), see "Bridge" below
7. Teardown detaches the control client only, never the session, and reaps
   it (`terminal/control.py` `terminate_client`: SIGHUP, then SIGKILL after a
   grace period, so a wedged client never lingers)

`terminal/routes.py` keeps what the bridge shares with the rest of Merlin:
`SessionReportState` (what each page displays and whether it is visible and
focused), `_client_views`, `looking_at`, `looking_clients`,
`displayed_targets` (read by the notifications suppression), `short_agent`,
`broadcast_control` (voice errors and the voice frames, through a per-page
outbox that drops a stalled page), `_sync_clipboard`, the transcription API,
uploads and the cwd API.

### tmux Session Restoration

Each WebSocket owns one tmux client. Session restoration is chosen before that
client starts, so a refresh never attaches to one session and visibly switches
to another afterwards.

The attach ladder (`terminal/tmux.py` `reconnect_argv`) is:

1. If the session sweep fails for an operational reason, close the WebSocket
   with code 1013 and retry. Do not attach or change browser identity.
2. If the browser supplied `(session_id, session_created)` and both fields
   exactly match a live sweep row, run `tmux -C attach -t <session_id>`.
3. If the identity is absent or stale and any session exists, run bare
   `tmux -C attach`. tmux chooses its most recently used session.
4. If no tmux server exists, run
   `tmux -C new-session -A -s merlin-dev -x 120 -y 40` (under
   `server_spawn_prefix`: its own transient scope under systemd, so the
   server outlives a Merlin restart).

The identity pair handles the important lifecycle cases:

- A rename changes the display name but keeps the id and creation time.
- A deleted session no longer matches, so the fallback runs and the next
  control frame replaces the stale browser value.
- A full tmux server restart may reuse `$0`, but the new session has a different
  creation time and does not match the old identity.
- Malformed or unavailable browser storage behaves exactly like no preference.

`sessionStorage` is intentional. `localStorage` would make every tab overwrite
one shared choice. With tab-scoped storage, two tabs may restore different tmux
sessions. If two tabs select the same session, normal tmux semantics still
apply: that session's selected window is shared by both clients.

A per-client switch (the Sessions panel, a deep link, a push tap) is a
`switch-client` on the page's own control connection, then `select-window`
when a window was named (its hooks run even when it is already current: a
deep link is a visit, and the hook clears the done pill).

### Agent identity and activity history

The terminal's provider state hooks own the current `@agent_state` pill and mint
the stable per-window `@agent_sid` plus pinned `@agent_cwd`. The built-in
[Timeline extension](agent-activity-timeline.md) reads that generic identity from
tmux in its own separately consented provider hooks. It never writes those core
options or the state pill; when identity is not ready, it uses a private actor
key without claiming liveness. It no-ops outside tmux. Historical capture is
therefore independent of the PTY bridge, and of a job run's supervision.

The state hooks find their window through `terminal/hooks/agent-window.sh`,
in this order: `$TMUX_PANE` when the hook descends from that pane's process
(in-process hooks); the process ancestors up to a pane's shell when a helper
stripped the environment (Claude Code 2.1.273 runs hooks through a helper
daemon whose children get neither `TMUX_PANE` nor `TMUX`, and the helper still
descends from the pane; this walk runs only when `MERLIN_TERMINAL_HOOKS` is
set, so a hook outside a Merlin terminal stays a no-op); then the hook
payload's session id and cwd, for Codex 0.165, which runs hooks in its
app-server daemon, a process shared by every Codex session of the user and
born with the environment of whichever pane started it: its `TMUX_PANE` is
that pane, dead or alive, and no ancestor is a pane. Nothing in the hook's
reach names the session's process (the daemon holds the per-thread locks
itself, the session metadata records no pid, nothing in the daemon's
environment names the session), so the hook binds only on what is tied to
the conversation: the window already stamped with the payload's session id
(`@agent_conv`) wins; at a job run's first prompt (the `[Job: id]` or
`[Triggered by webhook: id]` line Merlin wrote), the one window carrying
that `@job_id` in that cwd, still running (no `@job_exit`, no
`@job_finished`: a retained window of a run that failed before its first
prompt never qualifies) and with no conversation yet is bound and takes
the conversation; one job has one run in flight, so that window is the
run's. A `SessionStart` the daemon runs for a session no window
carries binds nothing: a pane's directory and start time are not proof that
it emitted the event, and losing a binding is better than overwriting
another conversation's identity. A hook that cannot prove its window stamps
none, and an inherited `TMUX_PANE` is never reinstated once its ancestry
check failed (it is the last resort for a hook with no payload at all). The
consequence: a Codex session a person opens in a tmux window under the
daemon gets no pills until Codex names its process to its hooks; a job's
Codex run does, through its prompt. A `$TMUX` whose socket is gone is
dropped first, so tmux reaches the default server. Codex runs its hooks
only in a directory it trusts; elsewhere no hook fires and the pills stay
blank. An agent the window's agent started (a `claude -p` from its shell
tool, a test, an eval script) inherits the pane and descends from it, but
binds nothing: on its way up to the pane's process the hook meets two agent
processes (`claude`, `codex`), whether the outer agent's tool shell stands
between them or execed itself away (Claude Code 2.1.322 runs a hook as its
own direct child, so the agent a hook runs for is always one of them). A
script named after an agent is a wrapper, not an agent: on Linux it bears
the script's name and `/proc/<pid>/exe` shows its interpreter; macOS `ps`
names it by its interpreter already, so it never matches (from Apple's
sources, not run on a Mac). An agent started under another name (its
versioned path, `node cli.js`) is not seen, which only misses nesting.
Measured on Claude Code 2.1.322 only; a Codex that ran hooks from its pane
(0.167 runs them in its daemon, where the rule never applies) or a spawning
launcher binary named after an agent would read as nesting.
Before this rule, such a run took the window's `@agent_transcript` and
`@agent_conv`, so the voice session read the run's conversation instead of
the agent's, and the workspace restore would have resumed it. The
Timeline's hook (`timeline/hook_runtime.py`) applies the same rule and
records nothing for such a run. Pinned by
`tests/unit/test_agent_state_hooks.py` and
`tests/unit/test_timeline_hook_runtime.py`.

The same `@agent_state` feeds the notifications feature: a server-side watcher
sweeps it every two seconds and turns transitions into browser notifications
and Web Push, with events riding the Sessions panel's poll. The bell in the
status bar, the `target` deep link, the `ping` control message and the
reconnect rule after a phone suspension are documented in
[`notifications.md`](notifications.md).

### Transcription API

`POST /api/terminal/transcribe`:
- Accepts multipart form: `file` (audio), `language`, `auto_enter` (`true`/`false`), `target` (`session:window_id`)
- Transcribes via `terminal/transcribe.py` (SaaS proxy → OpenAI Whisper → local faster-whisper)
- **`target` is captured at stop time.** The page reads the current tmux window the instant the recording stops (`currentSession:currentWindowId`, e.g. `merlin:@12`) and sends it with the audio. It is the window the text belongs in, whatever window or device is current when the upload finally lands. The pending IndexedDB record keeps the target so a retry uses the same one, never the window current at retry time.
- **With a valid `target`** (matching `^[^:\s]+:@\d+$`): returns `202 Accepted`, transcribes in the background, and injects the text straight into that tmux window with `board.sweep.send_literal(target, text)` (`tmux send-keys -t <session:window_id> -l -- <text>`), then, when `auto_enter`, `board.sweep.send_enter(target)` after a 150 ms gap. This addresses the window's active pane through tmux only: it moves no client and selects no window, so a user who switched away sees nothing move and the text waits in the window they left. It needs no open WebSocket. `_transcribe_and_inject()` runs every tmux call off the event loop.
- **Without a target, or a malformed one**: returns `200` with `{"text": "..."}` and the page writes the text into its own WebSocket (`sendToTerminal`). That socket already reaches the right device, so only the "lock the phone while it transcribes" convenience is lost. This is the right path for the first second after connect (before the first session frame) and for a pending record saved before this feature existed. The server never rewrites a missing target to some other window.
- **A vanished target is a logged loss.** If `send-keys` fails (the window closed between the stop tap and the injection, the session or tmux is gone), `_transcribe_and_inject()` logs a warning with the target and text length and writes nothing anywhere else. Recovery is the transcription-history epic's job, not this path's.
- **Final refusals are not retried.** The SaaS backend turns two portal answers into `TranscriptionRefused` subclasses: `402` (no trialing or active rig) raises `SubscriptionRequired`, `422` (the provider could not decode the audio) raises `AudioUnreadable`. On the `200` path the route relays that status with `{"error": "<reason>"}`, and the page prints the reason in yellow and drops the pending audio instead of entering the orange retry state, since resending the same audio cannot succeed. An empty upload never reaches the backend: the route answers `422` itself (iOS Safari can hand back an empty blob for a pending recording, and the page also refuses to save a zero-byte capture). Any other failure stays a `500`, which the page retries.
- **Background failures reach the pages.** On the `202` path the page has already been told the audio was accepted. When the background transcription then fails, `_broadcast_voice_error()` sends a `\x00{"type":"voice_error","message":...}` control frame to every connected terminal page (not only the recording one, since the user may have switched device), which prints it. A refusal carries its reason; any other failure says to check the Merlin logs.
- **Copy-mode guard**: injected text lands in a pane as if typed. If the pane is scrolled it is in tmux copy-mode, a modal keyboard grab, so those bytes would fire copy-mode key bindings (navigation, and some that kill the pane) instead of landing at the prompt. Before sending, `_transcribe_and_inject()` calls `board.sweep.exit_copy_mode(target)`, which reads that window's active pane and cancels the mode only if it is in one. Leaving copy-mode returns the view to the bottom, a deliberate tradeoff (dictation belongs at the prompt). The `200` fallback path injects client-side and is not covered by this guard.
- **Voice mode** uploads each utterance through this same route, with its window as target and `auto_enter`; when that window has a voice session, the route tells it the result (`_note_voice`). See [`voice-mode.md`](voice-mode.md).

## Authentication

Terminal access requires the same cookie auth as the rest of the dashboard. WebSocket auth is verified on the HTTP upgrade request. Unauthorized connections receive close code `4401`.

## Key Files

| File | Purpose |
|------|---------|
| `terminal/native.py`, `terminal/control.py` | The `/ws/terminal2` bridge, the tmux control-mode client and the client teardown |
| `terminal/routes.py` | The page and its old address, the transcription API (target-window injection), uploads, cwd, the clipboard sync, the shared session report state and the broadcast |
| `terminal/static/native/` | `core.js`, `renderer.js`, `engine.js`, `input.js`, `toolbar.js`, `page.js`, `native.css` |
| `templates/terminal-native.html` | The page |
| `templates/terminal-status-bar.html` and `.css`, `terminal-panels.html`, `terminal-head.html`, `terminal-chrome.html` | The chrome around the terminal |
| `templates/session-identity.js`, `clipboard-core.js` | The per-tab identity and the socket URL; the paste ladder (also in `clipboard-test.html`) |
| `terminal/transcribe.py` | Audio transcription (faster-whisper) |
| `terminal/tmux.py`, `terminal/tmux.conf` | The attach ladder, the terminal environment, Merlin's tmux configuration |
| `auth.py` | `verify_ws_cookie()` for WebSocket auth |
| `main.py` | Mounts the terminal router; `_check_tmux` at launch |

## What a terminal inherits

A tmux server gives its environment to every shell, agent and job it ever
starts, and keeps it for its whole life. So every path that can create one goes
through `terminal.tmux.terminal_process_env`, which withholds Merlin's
credentials (`paths.SECRET_ENV`) and its own
settings present in `config.env` (`paths.own_settings_in_config`: the server and
the CLI read the file themselves, and a copy would outlive any change to it).
Anything else a user keeps in `config.env`, an API key for their agents say,
passes through. At startup `scrub_server_env` removes the same names from a
running server's global environment, without ever starting one. A shell already
open keeps its copy until it closes, which is why `merlin restart`
(`server_control._relaunch_env`) drops those settings too.

A credential only the server's environment has (Merlin Cloud gives a container
its token that way) is kept, mode 0600, in `data/server-secrets.json`, rewritten
at every start. A restart reads it there, `merlin job url` for the public host,
and `merlin dashboard-url` for its login: only the operation that needs one reads it, so a program launched
from a pane (`merlin app run`, say) gets none.

## The terminal

### Bridge (`terminal/native.py`, `terminal/control.py`)

tmux renders and the page mirrors tmux's own grid of the displayed pane: rows
of its screen and of its history from `capture-pane -p -e -N -L -F`, each with
its wrap flag and an absolute number. Row `k` of the history is
`history_added - k`; the oldest row tmux holds is
`history_added - history_size` (not `history_collected`, which a reflow
leaves behind; after one, numbers can be negative). The bridge sends each
history row once, in order, reports rows tmux dropped before the page got them
as a gap, starts the page afresh on a history clear or reflow
(`history_generation`), and answers older rows by number on request. Updates
are pulled: at most one every 16 ms, except for the 100 ms after the page sent
input (the echo of a keystroke goes out at once), and only while the page has
room in its window of unacknowledged bytes. Nothing is ever paused in tmux. The
module docstring lists every guarantee and its test.

**Window size.** tmux's `window-size latest` follows the last active client, as
it does for attached terminals, but what a control client sends does not count
as activity: measured on 3.8, `send-keys`, `display-message`, `refresh-client`
and `select-pane` leave it where it was, `select-window` and `switch-client` do
not. So the page reports a gesture (`{"type": "active"}`, at most once a
second, on a key, tap or scroll) and the bridge runs `select-window -t :` on the
current window, only when the window's size is not the page's, since selecting a
window runs its hooks.

**A height change is not a reflow.** It moves rows between the screen and the
history without touching `history_added` or `history_generation` (measured on
3.8, 46 rows to 18: `history_size` up by 28, `history_added` unchanged), so no
reset comes and the bridge never sends the rows that left the screen. They are
missing from the page while the window is shorter and come back when it grows,
unless output scrolled meanwhile, which can leave a hole until the page reloads.
A phone's keyboard does not change the window's height (see `fit` in
`engine.js`), so this only concerns a desktop window made shorter. Numbering
rows by `history_collected + history_size` would remove the problem, at the
price of rewinding the page's confirmed rows when the window grows back.

### Renderer (`terminal/static/native/`)

- `core.js`: pure helpers, also run by node in `tests/js/native-core.test.js`.
  Frames are decoded by a scratch xterm.js parser that is never opened
  (`newDecoder`, with the Unicode 11 widths tmux uses:
  `static/vendor/xterm-addon-unicode11.min.js`), one capture as one stream
  (attributes carry from row to row, but a new row starts blank: tmux sends
  no reset for the cells a row leaves empty). `rowHtml` turns a decoded row into the
  HTML of one visual row; `findLinks` finds URLs over a logical line and
  `attachedLinks` reads the addresses programs attach to text (OSC 8), which
  tmux keeps in a capture. `linkTarget` keeps `http` and `https`, sends a
  `file:` address with no host (or `localhost`) to `/files/<path>`, and drops
  everything else. `decodeRows` empties xterm.js's link register before
  each capture, which otherwise keeps an entry per link of every capture.
- `renderer.js`: the DOM. A logical line is a `div.l`; each visual row in it a
  `span.r`, an inline-block as wide as the pane. The platform's own selection
  then copies a soft-wrapped line joined and breaks hard lines once, with no
  script (what iOS needs). Every row the page has, history then the screen, is
  one sequence of rows in chunks of 200 logical lines with
  `content-visibility: auto`; the screen is just its last rows. Rows never
  move and are never rebuilt: when output scrolls, the rows leaving the screen
  stay where they are (history now) and new rows are appended; a row's HTML is
  rewritten only when its content changed. That is what keeps a selection
  alive while output streams: the DOM collapses a range whose node is
  removed, even to be put back elsewhere. When logical lines must regroup (a
  wrap flag changed, a placeholder filled the start of a line already shown,
  rows dropped from the middle of a line), only the side no selection touches
  moves (filled rows go to the start of the line they continue, as older
  history does); with a selection on both sides the row waits, and when the
  selection goes its boundary is reconciled with its wrap flag as it is then
  (a flag that changed and changed back leaves nothing to do). Rows the page has no content for yet
  (the screen moved down by more than its height while history was on its
  way) are a placeholder of their exact height, filled in order; rows tmux
  dropped become a gap marker. Every row is one integer row height, and a
  chunk's skipped size is set exactly (no `auto`, whose remembered size would
  be stale for a chunk filled while off screen), so the spacer above the
  history (one row height for each row tmux holds that the page has not
  loaded) and the placeholders shrink by exactly the height of the rows that
  replace them: loading rows never moves the view and never sets `scrollTop`
  (which would stop an iOS momentum scroll). Rows past the end of a screen
  that got shorter are removed (`dropBelow`), or they would stay frozen at the
  bottom of the view. A view that follows the output stands at `followTop`:
  in a view shorter than the screen, the blank rows under the last used one
  are not text, so the used rows end at its bottom and, when they all fit,
  the screen's first row is at its top. The alternate screen is a layer
  over the scroller; leaving it shows the main screen where it was. The cursor
  is an overlay in its row whose character is generated content, so it is
  never copied.
- `engine.js`: the socket, frames drawn on the next animation frame and then
  acknowledged (a hidden page acknowledges nothing, so the bridge stops), lazy
  history, the size (the terminal font is loaded before the first size is
  sent; with a software keyboard up the window keeps its height, so the program
  is not redrawn each time the keyboard moves, except a full-screen one, which
  cannot be scrolled and gets the shorter height), the row at the bottom of the
  view kept there when its height changes, the desktop copy handler (Firefox serialises an empty selection over
  `content-visibility`, so the handler builds the text from the rows), and
  the mouse for programs that ask for it in the alternate screen, through
  pointer events only (a tap is reported once, whatever compatibility mouse
  events follow it): presses, drags, releases, motion once per cell entered
  in mode 1003 (button code 35), the wheel; a swipe is one wheel step per row
  height, arrow keys for a program that did not ask for the mouse; Shift keeps
  the browser's selection. SGR and UTF-8 reports go as text; X10 reports are
  bytes (a value past 95 is a byte above 127) and go as `{"type": "input",
  "hex": ...}`, which the bridge sends as they are, up to 223 (past it the
  encoding has no value: nothing is sent, as xterm.js does).
- `native.css`: the dashboard's terminal theme colours, styles, the cursor.
- `input.js`: the input, one real `textarea` (invisible: the terminal's own
  cursor shows where typing goes). On a touch screen it lies over the
  toolbar's Paste button, so a tap there pastes and a long press opens the
  system's Paste menu, while a long press on the terminal selects its text; a
  caret placed by a finger on the field is never sent as arrows (it goes back
  after what was typed before the next edit), and the menu's Cut is cancelled.
  It holds a sentinel of four no-break
  spaces on each side of the caret, so a software keyboard always has text to
  delete and a caret to move, and between them what was typed since the last
  reset. Every change is diffed against what the terminal already got
  (`Core.fieldDiff`): DEL back to the first character that differs, then the
  new tail, in one message. iOS rewrites its own text in place (a dictation
  hypothesis, an autocorrection, a prediction, held delete accelerating to
  words) and the terminal follows exactly; deleting into the leading sentinel
  deletes in the terminal and refills the field. Typed text is mapped per
  character before the comparison (`Core.typedText`: curly quotes, smart
  dashes and the ellipsis back to ASCII, no-break spaces to spaces, a line
  break to CR), so iOS turning a space into a no-break space costs nothing.
  Edits the diff cannot read are cancelled and mapped (`Core.inputAction`):
  Enter is CR, a word deleted with nothing typed is Ctrl+W, forward delete
  is `DC`, a deleted line is Ctrl+U, undo is ignored. A caret move (the
  space-bar trackpad) becomes one arrow per character, then a reset. A
  composition (an IME, an accent held on a key) is shown at the cursor
  (`.nt-preedit`) and sent once, when it commits. A key from the keyboard or
  the toolbar resets the field (what was typed no longer sits before the
  terminal's cursor); during a composition it first commits it with a blur,
  where it was typed, and a composition the browser keeps is dropped, never
  sent later against the moved cursor. When the displayed pane changes, the
  field is emptied and nothing is sent: its text was typed for the old pane.
  An Option dead key on a Mac is Meta with its letter, and an accent the
  browser still composes after it is not sent (the key typed after it is). Physical keys with no text
  (arrows, Escape, Tab, function keys, anything with Ctrl or Alt) are encoded
  from keydown as tmux key names (`Core.keyAction`; Alt is Meta, from the
  key's code when Option composes a character on a Mac), which tmux turns
  into the bytes of the pane's modes; text, plain Enter and plain Backspace go
  through the field. Keys typed while nothing that takes keys has the focus
  (a mouse selection took it, onto the page) go to the terminal and give the
  field the focus back; a key on a focused button, link, select or dialog
  (the voice console's, say) is that control's, as it is on any page. Ctrl+C over a selection is the browser's copy; Ctrl+Shift+C copies
  the selection or the last text a program copied; Ctrl+V (Cmd+V on a Mac,
  where Ctrl+V stays the program's) and Ctrl+Shift+V are the browser's own
  paste, read from the paste event (text through the bridge's paste path,
  images to the page). Suggestions (autocorrect, predictions, spellcheck)
  are always off: they rewrite commands, and the setting that turned them
  on was dropped.
- `toolbar.js`: the toolbar's keys (Esc, Tab, Shift+Tab, Enter, F3
  to F5, the new-window button, the arrows repeating while held) and its
  sticky Ctrl, Alt and Shift, shared with the input (`MerlinNativeInput.Modifiers`):
  a modifier applies to the next key from the toolbar, a physical keyboard
  or a software keyboard, then clears. No button takes the focus.

- `page.js`: the page around the engine, through the seams the rest of
  Merlin uses: `window.MerlinTerminal`
  (`switchSession`, `currentSession`, `currentWindow`, `currentTarget`,
  `focus`, `notice`, `lastPasteResult`, `probeStats`) for the Sessions panel,
  the bell, the voice console and the Apps panel; the `merlin:terminal-window`
  and `merlin:terminal-socket` events; the deep link and the push-tap relay;
  no window tabs (the founder dropped them: the bridge's `windows` frames are
  still sent, and the page ignores them); the paste button (the clipboard
  ladder), a right-click without a selection (with one, it is the browser's
  menu, so Copy is native; never on a touch screen, where a long press
  selects), image paste, the upload button and drag and drop typing the
  uploaded paths, `clipboard_sync` after every paste; a program's copy
  (merlin-clip, OSC 52, tmux's copy) written to the clipboard on a desktop and
  offered through the Copy pill on a touch screen (the gesture the clipboard
  needs); the cd and commits buttons; voice input (the target captured when
  the recording stops, IndexedDB retry, the 200 fallback typed here); the
  toolbar toggle; the
  reconnect with backoff, the ping probe after a suspension, the visibility
  report; the software keyboard following the visual viewport. With the
  keyboard up iOS only selects inside the focused field and does not follow a
  finger already down when the field lets go, so a finger held on the terminal
  makes the page select the word itself (a drag extends it), and iOS shows its
  own handles and Copy menu on that selection; the keyboard leaves and the
  layout waits for the selection to end, so the text stays under the finger.
  A message for the user (a voice or upload error, a tmux too old) is a notice
  over the terminal (`#nt-notice`): the page shows tmux's grid only.

The bridge accepts a key name only if tmux 3.8 parses it
(`native.valid_key_name`): tmux types a name it cannot parse as literal text
(measured: `C-Escape`, `C-BSpace`, `C-#`), which would put a key-name-like
word into the program.

What a row copies is exactly its text: trailing blanks of a row that ends its
line are left out, styled ones become empty boxes (`span.p`); a wide character
is one character in a 2ch box (`span.w`); a character from U+2000 on (box
drawing, symbols, Nerd Font icons) sits in a 1ch box (`span.n`) so a fallback
font cannot shift the columns after it.

Tests: `tests/unit/test_terminal_native.py` (the bridge, on a private tmux,
with differential checks of the page's decoder against a terminal fed the
whole stream, and every key name to the bytes of each pane mode),
`tests/js/native-core.test.js`, `tests/js/native-input.test.js` (the key
encoder per key, the diff model, the field driven by the event sequences a
phone produces), `tests/e2e/test_terminal_native.py` (the page in Chromium,
desktop and phone emulation, and Firefox for copy) and
`tests/e2e/test_terminal_native_input.py` (the exact bytes a recorder
program reads, in Chromium and Firefox),
`tests/e2e/test_terminal_native_parity.py` (the addresses, the tabs and pills,
the Sessions panel, paste, copy, uploads, cd, commits, voice, the probe), and
every other terminal e2e file (`test_terminal_paste.py`, `test_terminal_keyboard.py`,
`test_notifications.py`, `test_voice_console.py`, `test_app_terminal.py`, ...),
which open the page through `tests/e2e/terminal_support.py`.
