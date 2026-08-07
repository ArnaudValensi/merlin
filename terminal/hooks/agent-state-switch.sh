#!/bin/bash
# tmux window-change handler for the agent "done" pill (see terminal/tmux.conf).
# Clears the green ● (done) on window change, in two cases:
#   - the window you ARRIVE at  -> you visited an unread finished session
#   - the window you LEAVE      -> the agent finished while you were watching it
# Only ever clears a window whose state is exactly 'done'. busy/idle/unset are
# left alone, so a background window that went done while you were elsewhere
# stays green (still unread). Registered on session-window-changed (fires on
# ALL switch methods incl. mouse-clicking a pill in the web terminal) with
# after-select-window as redundancy. Uses $TMUX from the hook, so it targets
# the firing server. Never blocks tmux.

cur=$(tmux display-message -p '#{window_id}' 2>/dev/null) || exit 0
[ -n "$cur" ] || exit 0
prev=$(tmux show-option -sv @agent_state_prev_win 2>/dev/null)

clear_if_done() {
  local w=$1
  [ -n "$w" ] || return 0
  if [ "$(tmux show-option -wv -t "$w" @agent_state 2>/dev/null)" = "done" ]; then
    tmux set-option -w -t "$w" @agent_state idle 2>/dev/null
  fi
}

clear_if_done "$cur"                            # arrived (visit-clear)
[ "$prev" != "$cur" ] && clear_if_done "$prev"  # left    (leave-clear)

tmux set-option -s @agent_state_prev_win "$cur" 2>/dev/null
tmux refresh-client -S 2>/dev/null
exit 0
