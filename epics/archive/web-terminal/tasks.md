# Tasks: Web Terminal

## Phase 1: Core Terminal

### 1.1 WebSocket backend + PTY ✅
- [x] Create terminal module (like `notes/`) with WebSocket endpoint `/ws/terminal`
- [x] Auth verification on WebSocket upgrade (Basic Auth)
- [x] PTY creation: fork shell → `tmux new-session -A -s merlin-dev`
- [x] Bidirectional I/O: WebSocket ↔ PTY read/write loop
- [x] Terminal resize: receive cols/rows from client, call `pty.setwinsize()`
- [x] Clean disconnect handling (client closes tab, network drop)
- **Assignee:** Merlin
- **Depends on:** —
- **Validation:** WebSocket connects, shell prompt appears, commands work ✅

### 1.2 Frontend terminal page ✅
- [x] Create `templates/terminal.html` extending `base.html`
- [x] xterm.js integration (CDN: xterm, xterm-addon-fit, xterm-addon-web-links)
- [x] WebSocket connection to `/ws/terminal` with auth
- [x] Auto-fit terminal to viewport (fit addon + ResizeObserver)
- [x] Add route `/terminal` in dashboard.py
- [x] Add nav link in sidebar (`base.html`)
- [x] Dark theme matching dashboard (xterm.js theme config)
- **Assignee:** Merlin
- **Depends on:** 1.1
- **Validation:** Open `/terminal`, see shell, type commands, full tmux support ✅

### 1.3 Mobile responsive layout ✅
- [x] Terminal fills available viewport on mobile
- [x] Sidebar behavior unchanged (hamburger menu on mobile)
- [x] No horizontal scroll — terminal cols adapt to screen width
- [x] Test on small viewports (375px, 390px)
- **Assignee:** Merlin
- **Depends on:** 1.2
- **Validation:** Screenshots at mobile viewports show usable terminal ✅

## Phase 2: Mobile Toolbar

### 2.1 Key toolbar ✅
- [x] Toolbar row below/above terminal on touch devices
- [x] Modifier keys (sticky toggle): Esc, Tab, Ctrl, Alt, Shift
- [x] Function keys (direct send): F2, F3, F4, F5
- [x] Arrow keys: ←, ↑, ↓, →
- [x] Sticky modifier behavior: tap activates, next keypress includes it, then deactivates
- [x] Visual feedback: active modifier highlighted
- [x] Hidden on desktop by default (toggleable)
- **Assignee:** Merlin
- **Depends on:** 1.2
- **Validation:** On mobile, tap Ctrl then c sends Ctrl+C; F2 creates tmux window ✅

## Phase 3: Voice Input

### 3.1 Transcription API endpoint ✅
- [x] `POST /api/transcribe` — accepts audio blob, returns text
- [x] Reuse `transcribe.transcribe()` from `transcribe.py`
- [x] Auth: same as other API endpoints
- [x] Accept common audio formats (webm/opus from MediaRecorder)
- [x] Temporary file handling (save blob, transcribe, delete)
- **Assignee:** Merlin
- **Depends on:** —
- **Validation:** `curl` with audio file returns transcription ✅

### 3.2 Voice input UI ✅
- [x] Microphone button in toolbar or floating
- [x] MediaRecorder API: tap to start, tap to stop (or hold to record)
- [x] Recording indicator (visual feedback)
- [x] "Transcribing..." loading state
- [x] On result: inject transcribed text into terminal STDIN via WebSocket
- [x] Error handling: mic permission denied, transcription failure
- **Assignee:** Merlin
- **Depends on:** 1.2, 3.1
- **Validation:** Record voice, see text appear in terminal ✅

## Phase 4: Polish

### 4.1 Connection resilience ✅
- [x] Auto-reconnect WebSocket on disconnect (with backoff)
- [x] Visual indicator: connected/disconnected state
- [x] On reconnect: reattach to same tmux session (state preserved)
- **Assignee:** Merlin
- **Depends on:** 1.2
- **Validation:** Kill WebSocket, terminal reconnects and shows same session ✅

### 4.2 Screenshot validation ✅
- [x] Desktop viewport screenshots
- [x] Mobile viewport screenshots
- [x] Verify dark theme consistency
- [x] Verify toolbar layout on mobile
- **Assignee:** Merlin
- **Depends on:** all above
- **Validation:** Visual review of screenshots ✅
