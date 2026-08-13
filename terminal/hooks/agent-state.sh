#!/bin/bash
# Set the agent state on the tmux window of the current pane, for the
# status-bar pill (see terminal/tmux.conf window-status-format). Arg $1 = state:
#   idle  -> ○  (grey)     no turn running / waiting on nothing
#   busy  -> ◐  (amber)    the agent is working
#   ask   -> ?  (sky)      blocked on you: a dialog is open mid-turn
#   done  -> ●  (green)    finished, waiting on you
# No-op outside tmux or if tmux is unavailable, so it can never block a session.
#
# Engine-neutral: it only writes @agent_state. The Claude Code driver wires it
# through ~/.claude/settings.json hooks (installed by lib/skills.py):
#   UserPromptSubmit -> busy   Stop -> done   SessionStart -> idle

# Drain stdin (the hooks pipe JSON we don't need here) so the writer never
# blocks waiting on a pipe that no one closes.
cat >/dev/null 2>&1 || true

state="${1:-idle}"
[ -n "$TMUX_PANE" ] || exit 0
command -v tmux >/dev/null 2>&1 || exit 0

win=$(tmux display-message -p -t "$TMUX_PANE" '#{window_id}' 2>/dev/null) || exit 0
[ -n "$win" ] || exit 0

tmux set-option -w -t "$win" @agent_state "$state" 2>/dev/null
tmux refresh-client -S 2>/dev/null
exit 0
