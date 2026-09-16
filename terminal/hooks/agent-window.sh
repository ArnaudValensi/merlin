#!/bin/bash
# Shared by the agent hooks: the tmux window of the pane this hook runs in.
# Sourced, defines agent_window. Prints the window id or nothing.
#
# The normal path is $TMUX_PANE, which tmux gives every process it starts.
# Claude Code 2.1.273 runs hooks through a helper daemon whose children get
# a trimmed environment, without TMUX_PANE and without TMUX, so the hook would
# silently stamp nothing. The helper is still a descendant of the pane's
# shell, so the fallback walks up the ancestors until one is a pane process
# known to the tmux server. It is used only inside a Merlin terminal
# (MERLIN_TERMINAL_HOOKS is set for every process the terminal spawns), so a
# hook run outside one stays a no-op, and a test never reaches a real server.
agent_window() {
  local win="" p pid_line
  if [ -n "$TMUX_PANE" ]; then
    win=$(tmux display-message -p -t "$TMUX_PANE" '#{window_id}' 2>/dev/null)
  fi
  if [ -z "$win" ] && [ -n "$MERLIN_TERMINAL_HOOKS" ]; then
    local panes
    panes=$(tmux list-panes -a -F '#{pane_pid} #{window_id}' 2>/dev/null) || return 0
    p=$$
    while [ -n "$p" ] && [ "$p" != "1" ] && [ "$p" != "0" ]; do
      pid_line=$(printf '%s\n' "$panes" | awk -v pid="$p" '$1 == pid { print $2; exit }')
      if [ -n "$pid_line" ]; then win=$pid_line; break; fi
      p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
    done
  fi
  [ -n "$win" ] && printf '%s\n' "$win"
  return 0
}
