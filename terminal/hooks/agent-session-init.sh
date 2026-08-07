#!/bin/bash
# SessionStart companion for the Sessions board (see board/ module). Runs
# alongside agent-state.sh idle. On the current pane's tmux window it:
#   - mints a stable @agent_sid once, kept across resumes in the same window
#     (so a resumed session keeps its slot, name, and order on the board);
#   - pins @agent_cwd to the LAUNCH directory once and never again, so the
#     board groups the session by where it started even after the agent cd's.
# No-op outside tmux or if tmux is unavailable, so it can never block a session.

# Drain stdin (the hook pipes JSON we don't parse here).
cat >/dev/null 2>&1 || true

[ -n "$TMUX_PANE" ] || exit 0
command -v tmux >/dev/null 2>&1 || exit 0

win=$(tmux display-message -p -t "$TMUX_PANE" '#{window_id}' 2>/dev/null) || exit 0
[ -n "$win" ] || exit 0

# Mint a stable id once. Resume in the same window keeps the existing id.
sid=$(tmux show-option -wv -t "$win" @agent_sid 2>/dev/null)
if [ -z "$sid" ]; then
  sid=$(cat /proc/sys/kernel/random/uuid 2>/dev/null)
  [ -n "$sid" ] || sid="s${RANDOM}${RANDOM}"  # fallback if no kernel uuid
  tmux set-option -w -t "$win" @agent_sid "$sid" 2>/dev/null
fi

# Pin the launch cwd once. The hook runs in the session's cwd, which at
# SessionStart is where `claude` was launched. Never overwrite it afterwards.
cwd=$(tmux show-option -wv -t "$win" @agent_cwd 2>/dev/null)
if [ -z "$cwd" ]; then
  tmux set-option -w -t "$win" @agent_cwd "$PWD" 2>/dev/null
fi

exit 0
