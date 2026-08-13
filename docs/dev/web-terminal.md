# Web Terminal — Internals

Implementation reference for the browser-based terminal. The user guide is
[`docs/terminal.md`](../terminal.md); this doc covers how it is built.

## Architecture

```
Browser (xterm.js)
  ↕ WebSocket (/ws/terminal)
Dashboard (FastAPI)
  ↕ asyncio PTY (openpty)
tmux session ("merlin")
  ↕
zsh shell
```

## Frontend

### xterm.js Configuration

- **Font**: Geist Mono (Google Fonts CDN), 11px
- **Theme**: Dracula-inspired (matches dashboard dark theme)
- **Scrollback**: 5000 lines
- **Addons**: FitAddon (auto-resize), WebLinksAddon (clickable URLs)

### Connection

```javascript
const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
ws = new WebSocket(`${proto}//${location.host}/ws/terminal`);
```

- Auto-reconnect with exponential backoff (1s → 30s max)
- Status indicator: green dot (connected), yellow (connecting), red (disconnected)
- Auth failure (close code 4401) stops reconnection

### Resize Handling

Terminal dimensions sync on:
- Window resize
- Orientation change (mobile)
- ResizeObserver on container
- Initial WebSocket connection

Sends resize message:
```json
{"type": "resize", "cols": 80, "rows": 24}
```

### Touch Gesture Implementation

A transparent `#touch-overlay` div sits on top of the terminal and captures all touch events on mobile (`@media (pointer: coarse)`). On desktop, the overlay has `pointer-events: none` so mouse events pass through to xterm.js normally.

The overlay exists because xterm.js's DOM renderer removes/recreates `<span>` elements during re-render — if a touch target element is removed mid-gesture, the browser fires `touchcancel` and kills the gesture. The overlay is a single stable element that never gets re-rendered.

All touch gestures are translated to SGR mouse sequences sent directly to the PTY:

**Vertical swipe → scroll:**
- `\x1b[<64;1;1M` — scroll up
- `\x1b[<65;1;1M` — scroll down
- 20px per line threshold

**Tap → click:**
- `\x1b[<0;col;rowM` — button press at cell position
- `\x1b[<0;col;rowm` — button release
- Also calls `term.focus()` to open virtual keyboard

**Horizontal drag → select:**
- `\x1b[<0;col;rowM` — button press at start position
- `\x1b[<32;col;rowM` — motion events during drag
- `\x1b[<0;col;rowm` — button release at end position

Gesture detection: after 8px of movement, vertical = scroll, horizontal = select.

### Clipboard Plumbing

All copy operations go through `merlin-clip`, which writes OSC 52 directly to the tmux client TTY. All paste paths use one unified handler (`handlePaste()`), supporting text and images. Images upload to `/tmp/merlin-clipboard/` and the path is injected into the terminal.

The read itself lives in `terminal/templates/clipboard-core.js` — a Jinja partial included by both `terminal.html` and `clipboard-test.html`, so the diagnostic page always runs exactly what the terminal runs. It tries `clipboard.read()` (the only API that returns images), then falls back to `clipboard.readText()` whenever `read()` came back with nothing pasteable. Do not add a second copy of that ladder anywhere: `tests/unit/test_clipboard_core.py` fails if you do, and the last time the logic existed twice the diagnostic page reported success on the path where the terminal was broken. Behaviour is covered by `tests/js/clipboard-core.test.js` (per-browser cases) and `tests/e2e/test_terminal_paste.py` (real browser).

**tmux configuration** (`terminal/tmux.conf`, NOT `~/.tmux.conf`): the web terminal starts tmux with `terminal/tmux.conf`, which loads first and sources `~/.tmux.conf` at the end. Mouse copy is handled by `copy-pipe-and-cancel "merlin-clip copy"` — copy text and send it to the browser clipboard in one step.

User-facing copy/paste recipes (shortcuts, pills, NeoVim config, `/terminal/clipboard-test`) live in [`docs/terminal.md`](../terminal.md).

## Backend

### WebSocket Endpoint (`/ws/terminal`)

In `terminal/routes.py`:

1. Verify auth via session cookie
2. Fork a PTY (`pty.fork()`) running `tmux new-session -A -s merlin-dev`
3. Wrap the master fd in a `PtyBridge` (`terminal/pty_bridge.py`)
4. Bidirectional relay:
   - WebSocket → PTY: forward input via `bridge.write()`
   - PTY → WebSocket: forward output via `bridge.read()` through an
     incremental UTF-8 decoder (multibyte sequences can split across reads)
5. Handle resize messages (JSON with `type: "resize"`)
6. Clean up on disconnect: close the bridge, then `terminate_client()`
   SIGHUPs the tmux client, waits for it to exit, and escalates to SIGKILL

### PTY Bridge (`terminal/pty_bridge.py`)

All PTY I/O goes through the event loop on a **non-blocking** master fd
(`loop.add_reader` / `add_writer`). This is a hard constraint, not a style
choice: a blocking `os.read()` in an executor thread deadlocks in the macOS
kernel if `os.close()` runs concurrently on the same PTY fd. Both threads
enter uninterruptible sleep, the process survives SIGKILL, and the frozen
event loop takes the SaaS tunnel down with it (2026-07-02 outage). With the
bridge, no thread is ever inside a PTY syscall, so teardown cannot deadlock.

Properties the tests pin down (`tests/unit/test_pty_bridge.py`):

- **Backpressure**: reading pauses at a high-water mark when the WebSocket
  consumer falls behind; the kernel PTY buffer then throttles the child.
- **Write stalls**: a write that can't progress (child stopped draining)
  gives up after a timeout instead of waiting forever.
- **Teardown**: `close()` is synchronous, idempotent, wakes any waiting
  reader/writer, and can never block.

`add_reader`/`add_writer` are used instead of asyncio pipe transports
deliberately: they are loop-agnostic (uvloop supports them on any fd,
while its pipe transports reject TTYs).

### tmux Session

- **Session name**: `merlin`
- **Flag**: `-A` (attach if exists, create if not)
- Terminal state persists across page reloads
- Multiple browser tabs share the same tmux session

### Transcription API

`POST /api/terminal/transcribe`:
- Accepts multipart form: `file` (audio), `language`, `auto_enter` (`true`/`false`)
- Transcribes via `transcribe.py` (SaaS proxy → OpenAI Whisper → local faster-whisper)
- **With PTY registered**: returns `202 Accepted`, transcribes in background, writes text to the PTY through `bridge.write()` via `_transcribe_and_inject()`
- **Without PTY**: returns `200` with `{"text": "..."}` for client-side injection (fallback)
- PTY registry: `register_pty()` / `unregister_pty()` / `get_pty_bridge()` in `routes.py`

## Authentication

Terminal access requires the same cookie auth as the rest of the dashboard. WebSocket auth is verified on the HTTP upgrade request. Unauthorized connections receive close code `4401`.

## Key Files

| File | Purpose |
|------|---------|
| `terminal/routes.py` | WebSocket endpoint, PTY registry, transcription API |
| `terminal/pty_bridge.py` | Non-blocking PTY I/O, tmux client termination |
| `templates/terminal.html` | xterm.js frontend, toolbar, clipboard, voice input |
| `transcribe.py` | Audio transcription (faster-whisper) |
| `auth.py` | `verify_ws_cookie()` for WebSocket auth |
| `main.py` | Mounts terminal router |
