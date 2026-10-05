#!/bin/bash
# SessionStart companion for the Sessions board (see board/ module). Runs
# alongside agent-state.sh idle. On the current pane's tmux window it:
#   - mints a stable @agent_sid once, kept across resumes in the same window
#     (so a resumed session keeps its slot, name, and order on the board);
#   - pins @agent_cwd to the LAUNCH directory once and never again, so the
#     board groups the session by where it started even after the agent cd's;
#   - stamps @agent_provider ($1: claude|codex) and @agent_conv, the provider's
#     conversation id (`session_id` in the hook JSON on stdin). Unlike the sid,
#     @agent_conv is overwritten on every SessionStart: /clear and resume change
#     the conversation. The workspace snapshot reads it to resume the agent
#     after a restart (see workspace/).
# No-op outside tmux or if tmux is unavailable, so it can never block a session.

provider="$1"
case "$provider" in claude|codex) ;; *) provider="" ;; esac

# Read the hook JSON. Only `session_id` is used: a uuid-like token, matched with
# a bash regex so the hook needs no jq.
payload=$(cat 2>/dev/null) || payload=""
conv=""
re='"session_id"[[:space:]]*:[[:space:]]*"([A-Za-z0-9_-]+)"'
if [[ $payload =~ $re ]]; then
  conv="${BASH_REMATCH[1]}"
fi

command -v tmux >/dev/null 2>&1 || exit 0

# The window of this pane: $TMUX_PANE, or the ancestor walk when a helper
# process stripped the environment (see agent-window.sh).
. "$(dirname "$0")/agent-window.sh"
win=$(agent_window)
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

# The live conversation, refreshed on every start / resume / clear.
if [ -n "$provider" ] && [ -n "$conv" ]; then
  tmux set-option -w -t "$win" @agent_provider "$provider" 2>/dev/null
  tmux set-option -w -t "$win" @agent_conv "$conv" 2>/dev/null
fi

exit 0
