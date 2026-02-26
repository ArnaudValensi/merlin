# 2026-02-14 — Design Session

## Context

The user wants to replace his SSH + tmux workflow with a browser-based terminal in the dashboard. Currently he SSHes into the Docker container, opens tmux, and runs `claude` from `merlin/`. This is cumbersome, especially on mobile.

## Key Design Decisions

1. **Full PTY terminal, not just Claude** — he needs tmux, git, navigation, not just a Claude-only interface
2. **Persistence via tmux** — backend attaches to named session `merlin-dev` (create if missing). Close tab = tmux keeps running. Reopen = reattach. Backwards compatible with SSH.
3. **xterm.js + WebSocket** — xterm.js renders the terminal (same as VS Code). FastAPI WebSocket endpoint bridges to PTY. No external binary (no ttyd).
4. **Mobile toolbar like Termius** — sticky modifier keys (Esc, Tab, Ctrl, Alt, Shift) + function keys (F2-F5 for tmux) + arrow keys. Hidden on desktop by default.
5. **Voice input** — MediaRecorder → `POST /api/transcribe` → existing Faster Whisper (`transcribe.py`, medium model, French only for now) → inject text into terminal STDIN
6. **Transcription language** — French only for now. Toggle may come later.
7. **Auth** — existing HTTP Basic Auth, verified on WebSocket upgrade
8. **Module structure** — like `notes/`, self-contained module mounted in dashboard.py

## Implementation Order

Phase 1: Core terminal (WebSocket + PTY + xterm.js + tmux)
Phase 2: Mobile toolbar
Phase 3: Voice input
Phase 4: Polish (reconnect, screenshots)

Start with task 1.1 (WebSocket backend + PTY), then 1.2 (frontend page).
