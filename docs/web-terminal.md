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

Full clipboard interop between browser and container, on mobile and desktop. All copy operations go through `merlin-clip` which writes OSC 52 directly to the tmux client TTY.

**Desktop keyboard shortcuts:**

| Shortcut | Action |
|---|---|
| **Ctrl+V** | Paste from OS clipboard |
| **Ctrl+Shift+C** | Copy selection to OS clipboard |
| **Ctrl+Shift+V** | Paste (alternative, via Clipboard API) |
| **Ctrl+C** | SIGINT (interrupt process) — NOT copy |

Note: Ctrl+C is always SIGINT in a terminal. Ctrl+Shift+C/V is the standard Linux terminal convention (same as GNOME Terminal, Alacritty, Kitty).

**Copy (container → browser):**
- **Mouse select** (desktop): drag to select → release → copied to OS clipboard via merlin-clip
- **Touch select** (mobile): drag → green "Copy" pill appears → tap to copy
- **Ctrl+Shift+C** (desktop): copy current xterm.js selection or last copied text
- **NeoVim yank**: `yy`, visual `y` → copies to browser clipboard via merlin-clip
- **`merlin-clip copy`** / **`pbcopy`**: pipe text to copy — `echo "text" | pbcopy`

**Paste (browser → container):**
- **Ctrl+V** (desktop): paste text or image from OS clipboard
- **Ctrl+Shift+V** (desktop): paste (alternative shortcut, same behavior)
- **Long-press** (mobile): hold 500ms → blue "Paste" pill → tap to paste text or image
- **Paste button**: clipboard icon in toolbar → reads clipboard and pastes
- **Right-click** (desktop): right-click on terminal → pastes clipboard
- **`merlin-clip paste`** / **`pbpaste`**: outputs last pasted text — for scripts and editors

All paste paths use the same unified handler (`handlePaste()` via `navigator.clipboard.read()`), which supports both text and images.

**Image clipboard:**
- **Ctrl+V / paste button / long-press** with image in clipboard: image uploads to `/tmp/merlin-clipboard/`, path injected in terminal
- **Image button**: tries clipboard image first, falls back to file picker
- **Drag and drop** (desktop): drop image onto terminal

**`merlin-clip` — clipboard bridge:**

```bash
echo "hello" | merlin-clip copy   # copy to browser clipboard
merlin-clip paste                  # output last pasted text
echo "hello" | merlin-clip         # shorthand (pipe = copy)
```

Aliases: `pbcopy` = `merlin-clip copy`, `pbpaste` = `merlin-clip paste`

**tmux configuration** (`terminal/tmux.conf`, NOT `~/.tmux.conf`):

The web terminal starts tmux with `terminal/tmux.conf` which loads first, then sources `~/.tmux.conf` at the end. Mouse copy is handled by `copy-pipe-and-cancel "merlin-clip copy"` — this copies text and sends it to the browser clipboard in one step.

**NeoVim integration:**

Add to your NeoVim config (e.g., `lua/config/options.lua`):

```lua
if vim.fn.executable('merlin-clip') == 1 then
    vim.g.clipboard = {
        name = 'merlin',
        copy = { ['+'] = {'merlin-clip', 'copy'}, ['*'] = {'merlin-clip', 'copy'} },
        paste = { ['+'] = {'merlin-clip', 'paste'}, ['*'] = {'merlin-clip', 'paste'} },
    }
end
vim.o.clipboard = 'unnamedplus'
```

When `merlin-clip` is installed, all yanks go to the browser clipboard. Outside Merlin, NeoVim uses the OS clipboard as usual.

**Requirements:**
- HTTPS required (Clipboard API unavailable over HTTP)
- Mobile: clipboard access needs a user gesture (the pills provide this)

**Troubleshooting:** See `docs/merlin-cli/clipboard.md` for detailed technical decisions, debugging methodology, and the `/clipboard-test` diagnostic page.

### Voice Input

Microphone button records audio and sends to `/api/transcribe`:
- Records via MediaRecorder API (WebM/Opus preferred)
- **Server-side injection**: when a PTY is active (WebSocket connected), returns 202 and injects transcribed text directly into the terminal PTY — phone can disconnect after upload
- **Fallback**: when no PTY is available, returns 200 with text in response body for client-side injection
- Upload progress indicator on mic button (XHR `upload.onprogress`)
- Auto-enter toggle (↵ button): appends carriage return after transcription to submit automatically
- Language selector (saved in localStorage)
- Requires HTTPS (mic API unavailable over HTTP)
- Visual states: idle → recording (red pulse) → uploading (blue fill) → done (green flash)

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
- Accepts multipart form: `file` (audio), `language`, `auto_enter` (`true`/`false`)
- Transcribes via `transcribe.py` (SaaS proxy → OpenAI Whisper → local faster-whisper)
- **With PTY registered**: returns `202 Accepted`, transcribes in background, writes text directly to PTY fd via `_transcribe_and_inject()`
- **Without PTY**: returns `200` with `{"text": "..."}` for client-side injection (fallback)
- PTY registry: `register_pty()` / `unregister_pty()` / `get_pty_fd()` in `routes.py`

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
