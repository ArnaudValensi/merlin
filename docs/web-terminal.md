# Web Terminal

Reference documentation for the browser-based terminal that provides shell access via the dashboard.

## Overview

The web terminal provides a full terminal emulator in the browser using xterm.js, connected to a server-side PTY via WebSocket. It attaches to a persistent `tmux` session, so terminal state survives page reloads and reconnections.

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

### Touch Gestures

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

### Mobile Toolbar

A toolbar with virtual keys shown on touch devices:

**Modifier keys** (sticky — toggle on/off):
- Ctrl, Alt, Shift

**Direct keys**:
- Esc, Tab, Shift+Tab
- F2, F3, F4, F5 (tmux window management)
- Arrow keys (with modifier support)

Modifiers are cleared after a keypress. The toolbar is hidden by default on desktop, shown on touch devices.

### Clipboard (Copy & Paste)

Copy and paste integration for mobile (and desktop when toolbar is visible):

**Copy via OSC 52:**
- tmux is configured with `set -g set-clipboard on` (in `~/.tmux.conf`)
- When text is selected/copied in tmux, it sends an OSC 52 escape sequence: `\x1b]52;c;<base64>\x1b\\`
- xterm.js intercepts this via `term.parser.registerOscHandler(52, ...)`
- The base64 payload is decoded and written to the browser clipboard via `navigator.clipboard.writeText()`
- Status bar briefly shows "Copied!" in green for 1.5s

**Paste via toolbar button:**
- A clipboard icon button (`#paste-btn`) in the toolbar reads from `navigator.clipboard.readText()` on click
- The clipboard text is sent to the terminal via `sendToTerminal()`
- On error (permission denied), a red bracketed message is written to the terminal

**Requirements:**
- Both copy and paste require **HTTPS** (available via Cloudflare Tunnel)
- `readText()` (paste) requires a user gesture (the button click satisfies this) and may prompt for permission on first use
- Desktop keyboard Ctrl+C/Ctrl+V work natively via xterm.js (no special handling needed)

### Voice Input

Microphone button records audio and sends to `/api/transcribe`:
- Records via MediaRecorder API (WebM/Opus preferred)
- Transcribes server-side via faster-whisper
- Injects transcribed text directly into terminal
- Language selector (saved in localStorage)
- Requires HTTPS (mic API unavailable over HTTP)
- Visual states: idle → recording (red pulse) → transcribing (yellow)

## Backend

### WebSocket Endpoint (`/ws/terminal`)

In `terminal/routes.py`:

1. Verify auth via session cookie
2. Create PTY pair (`os.openpty()`)
3. Spawn process: `tmux new-session -A -s merlin` (attach or create)
4. Bidirectional relay:
   - WebSocket → PTY: forward input
   - PTY → WebSocket: forward output
5. Handle resize messages (JSON with `type: "resize"`)
6. Clean up on disconnect

### tmux Session

- **Session name**: `merlin`
- **Flag**: `-A` (attach if exists, create if not)
- Terminal state persists across page reloads
- Multiple browser tabs share the same tmux session

### Transcription API

`POST /api/transcribe`:
- Accepts multipart form with audio file + language
- Transcribes via `transcribe.py` (faster-whisper)
- Returns `{"text": "transcribed text"}` or `{"error": "..."}`

## Authentication

Terminal access requires the same cookie auth as the rest of the dashboard. WebSocket auth is verified on the HTTP upgrade request. Unauthorized connections receive close code `4401`.

## Key Files

| File | Purpose |
|------|---------|
| `terminal/routes.py` | WebSocket endpoint, PTY management |
| `templates/terminal.html` | xterm.js frontend, toolbar, clipboard, voice input |
| `transcribe.py` | Audio transcription (faster-whisper) |
| `auth.py` | `verify_ws_cookie()` for WebSocket auth |
| `main.py` | Mounts terminal router |
