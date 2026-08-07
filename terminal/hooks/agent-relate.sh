#!/bin/bash
# Stamp a family link on a freshly spawned agent window, for the Sessions board.
# Meant to be called right after `tmux new-window` by a spawner (fork/handoff):
# it records which session this one descends from and how.
#
# Usage: agent-relate.sh <new_window_id> <relation> [parent_window_id]
#   <new_window_id>    the window to stamp (e.g. captured via new-window -P -F '#{window_id}')
#   <relation>         sibling | child
#   [parent_window_id] the spawner's window; defaults to the current window
#
# It writes @agent_parent (the PARENT's @agent_sid, a stable id — not a window
# id, which tmux reuses) and @agent_relation on the new window. The board reads
# these to place the session: siblings lay out flat within the family, children
# nest one level under their parent, and hierarchy wins over project grouping.
# No-op outside tmux or on bad args, so it can never break a spawn.

new_win="$1"
relation="$2"
parent_win="${3:-}"

[ -n "$new_win" ] || exit 0
case "$relation" in
  sibling | child) ;;
  *) exit 0 ;;
esac
command -v tmux >/dev/null 2>&1 || exit 0

# Resolve the parent window (default: the window this script runs from).
if [ -z "$parent_win" ] && [ -n "$TMUX_PANE" ]; then
  parent_win=$(tmux display-message -p -t "$TMUX_PANE" '#{window_id}' 2>/dev/null)
fi

parent_sid=""
[ -n "$parent_win" ] && parent_sid=$(tmux show-option -wv -t "$parent_win" @agent_sid 2>/dev/null)

[ -n "$parent_sid" ] && tmux set-option -w -t "$new_win" @agent_parent "$parent_sid" 2>/dev/null
tmux set-option -w -t "$new_win" @agent_relation "$relation" 2>/dev/null
exit 0
