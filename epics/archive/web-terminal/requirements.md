# Epic: Web Terminal

## Overview

Add a web-based terminal page to the Merlin dashboard, replacing the SSH + tmux workflow with a browser-accessible terminal. Accessible from any device (mobile-first), with a voice-to-STDIN feature using the existing Faster Whisper transcription.

## Goals

1. **No more SSH** — Open the dashboard, get a terminal. No SSH client, no Termius dependency
2. **Session persistence** — Close the tab, come back later, same terminal state. Powered by tmux
3. **Mobile-first** — Fully usable from a phone, with a touch-friendly modifier key toolbar
4. **Voice input** — Record audio, transcribe to text (Faster Whisper), inject into terminal STDIN
5. **Integrated** — Part of the dashboard, same process, same auth, same port

## Architecture

### Terminal Stack

```
┌─────────────────────────────────────┐
│  Browser                            │
│  ┌───────────────────────────────┐  │
│  │  xterm.js                     │  │
│  │  (terminal emulator)          │  │
│  └──────────┬────────────────────┘  │
│             │ WebSocket              │
│  ┌──────────┴────────────────────┐  │
│  │  Mobile toolbar               │  │
│  │  Esc Tab Ctrl Alt Shift       │  │
│  │  F2 F3 F4 F5  ← ↑ ↓ →       │  │
│  └───────────────────────────────┘  │
│  ┌───────────────────────────────┐  │
│  │  🎤 Voice input button        │  │
│  └───────────────────────────────┘  │
└─────────────────────────────────────┘
              │
              │ WebSocket (wss://)
              ▼
┌─────────────────────────────────────┐
│  FastAPI (dashboard.py)             │
│  ┌───────────────────────────────┐  │
│  │  /ws/terminal                 │  │
│  │  WebSocket endpoint           │  │
│  │  Auth on connect              │  │
│  └──────────┬────────────────────┘  │
│             │ PTY I/O                │
│             ▼                        │
│  ┌───────────────────────────────┐  │
│  │  PTY → tmux attach            │  │
│  │  (or tmux new-session         │  │
│  │   if none exists)             │  │
│  └───────────────────────────────┘  │
└─────────────────────────────────────┘
```

### Persistence via tmux

- Backend targets a named tmux session: `merlin-dev`
- On WebSocket connect: `tmux attach-session -t merlin-dev || tmux new-session -s merlin-dev`
- The PTY connected to the WebSocket is the `tmux attach` process
- Close the tab → tmux session keeps running
- Reconnect → reattach, full state preserved
- Backwards compatible: can still SSH and `tmux attach -t merlin-dev`

### Voice Input

- Browser: `MediaRecorder` API captures audio from mic
- Send audio blob to `POST /api/transcribe`
- Backend runs it through existing `transcribe.transcribe()` (Faster Whisper, medium model, French)
- Returns transcribed text
- JS injects text into terminal STDIN via WebSocket

Language is French only for now — may add a toggle later.

## UX Design

### Page Layout

New dashboard page at `/terminal`. Mobile-first, same dark theme.

- **Terminal area**: xterm.js, takes up most of the viewport
- **Mobile toolbar**: Row of touch-friendly key buttons (below or above the terminal)
- **Voice button**: Floating or in the toolbar — hold to record, release to transcribe and inject

### Mobile Toolbar

Shown on touch devices (or toggleable on desktop). Buttons:

**Modifiers** (sticky toggle — tap to activate, next keypress includes it):
- `Esc` `Tab` `Ctrl` `Alt` `Shift`

**Function keys** (direct send):
- `F2` `F3` `F4` `F5`

**Navigation**:
- `←` `↑` `↓` `→`

Modifiers highlight when active and deactivate after the next keypress (like Termius behavior).

### Voice Input UX

- Microphone button (floating or in toolbar)
- Tap and hold to record (or tap to start, tap to stop)
- Visual feedback: recording indicator (pulsing red dot or similar)
- On release: sends audio to backend, shows brief "transcribing..." state
- Transcribed text is injected into terminal STDIN
- French only for now

## Technical Details

### Dependencies

**Frontend (CDN):**
- `xterm.js` — terminal emulator
- `xterm-addon-fit` — auto-resize terminal to container
- `xterm-addon-web-links` — clickable URLs

**Backend (Python):**
- `websockets` — FastAPI native WebSocket support (already in FastAPI)
- `pty` module — standard library, PTY creation
- Existing `transcribe.py` for voice input

### Auth

- WebSocket `/ws/terminal` verifies Basic Auth credentials on the initial HTTP upgrade
- Same `DASHBOARD_USER` / `DASHBOARD_PASS` as the rest of the dashboard
- `POST /api/transcribe` uses same auth as other API endpoints

### Terminal Resize

- xterm.js `fit` addon detects container size changes
- On resize, send new dimensions (cols, rows) over WebSocket
- Backend calls `pty.setwinsize()` to resize the PTY
- Handles orientation changes on mobile

## Non-Goals (for now)

- Multiple terminal tabs (can use tmux windows via F2/F3/F4)
- Language toggle for transcription (French only, add later)
- Terminal session recording/playback
- File upload/download through terminal
