#!/bin/bash
# Shared by the agent hooks: the tmux window of the agent session this hook
# runs for. Sourced, defines agent_window. Prints the window id or nothing.
#
#   agent_window [session_id] [cwd]
#
# The normal path is $TMUX_PANE, which tmux gives every process it starts,
# checked against the hook's own ancestry: a hook run from inside the pane
# (Claude Code's in-process hooks, Codex before its daemon) descends from the
# pane's process. Two agents break that:
#
# - Claude Code 2.1.273 runs hooks through a helper daemon whose children get
#   a trimmed environment, without TMUX_PANE and without TMUX. The helper is
#   still a descendant of the pane's shell, so the fallback walks up the
#   ancestors until one is a pane process known to the tmux server. It is
#   used only inside a Merlin terminal (MERLIN_TERMINAL_HOOKS is set for every
#   process the terminal spawns), so a hook run outside one stays a no-op,
#   and a test never reaches a real server.
# - Codex 0.165 runs hooks in its app-server daemon, a process shared by every
#   Codex session of the user, with the daemon's own environment: TMUX_PANE
#   and TMUX are those of whichever pane started the daemon, dead or alive,
#   and no ancestor is a pane. The hook payload names the session and its
#   cwd, which identify the window instead: the window already stamped with
#   this conversation (@agent_conv, by the session init hook), else the pane
#   in that directory whose newest process is a Codex TUI (SessionStart fires
#   right after it started). A $TMUX whose socket is gone is dropped first, so
#   tmux reaches the default server.
#
# The last resort is TMUX_PANE as given, for a hook with no payload.

# Dropped at source time, in the hook's own shell, so every tmux call of the
# hook benefits (agent_window runs in a command substitution).
if [ -n "$TMUX" ] && [ ! -S "${TMUX%%,*}" ]; then
  unset TMUX
fi

agent_window() {
  local conv="$1" cwd="$2" win="" p pid_line pane_pid pane_win
  if [ -n "$TMUX_PANE" ]; then
    pane_win=$(tmux display-message -p -t "$TMUX_PANE" '#{window_id} #{pane_pid}' 2>/dev/null)
    pane_pid="${pane_win#* }"
    pane_win="${pane_win%% *}"
    if [ -n "$pane_win" ] && _agent_descends_from "$pane_pid"; then
      win="$pane_win"
    fi
  fi
  if [ -z "$win" ] && [ -n "$MERLIN_TERMINAL_HOOKS" ]; then
    local panes
    panes=$(tmux list-panes -a -F '#{pane_pid} #{window_id}' 2>/dev/null) || panes=""
    p=$$
    while [ -n "$p" ] && [ "$p" != "1" ] && [ "$p" != "0" ]; do
      pid_line=$(printf '%s\n' "$panes" | awk -v pid="$p" '$1 == pid { print $2; exit }')
      if [ -n "$pid_line" ]; then win=$pid_line; break; fi
      p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
    done
  fi
  if [ -z "$win" ] && [ -n "$conv" ]; then
    win=$(tmux list-windows -a -F '#{window_id} #{@agent_conv}' 2>/dev/null \
      | awk -v c="$conv" '$2 == c { print $1; exit }')
  fi
  if [ -z "$win" ] && [ -n "$cwd" ]; then
    win=$(_agent_window_by_cwd "$cwd")
  fi
  if [ -z "$win" ] && [ -n "$pane_win" ]; then
    win="$pane_win"
  fi
  [ -n "$win" ] && printf '%s\n' "$win"
  return 0
}

# True when this hook process descends from $1 (a pane's process).
_agent_descends_from() {
  local target="$1" p=$$
  [ -n "$target" ] || return 1
  while [ -n "$p" ] && [ "$p" != "1" ] && [ "$p" != "0" ]; do
    [ "$p" = "$target" ] && return 0
    p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
  done
  return 1
}

# The window of the pane in directory $1 whose newest process is a Codex TUI
# (a process named codex that is not the app-server daemon), a window not yet
# stamped with a conversation first. Nothing when no pane qualifies, or when
# ps cannot report elapsed times (then the caller's last resort applies).
_agent_window_by_cwd() {
  local cwd="$1" panes procs
  panes=$(tmux list-panes -a -F '#{pane_pid} #{window_id} #{@agent_conv} #{pane_current_path}' 2>/dev/null) || return 0
  [ -n "$panes" ] || return 0
  procs=$(ps -eo pid=,ppid=,etimes=,comm=,args= 2>/dev/null) || return 0
  printf '%s\n%s\n' "$panes" "---" "$procs" | awk -v cwd="$cwd" '
    BEGIN { section = "panes" }
    section == "panes" {
      if ($0 == "---") { section = "procs"; next }
      pane_pid = $1; win = $2; conv = $3
      path = $0; sub(/^[^ ]+ [^ ]+ [^ ]* /, "", path)
      if (path == cwd) { pane[pane_pid] = win; stamped[pane_pid] = (conv != "") }
      next
    }
    {
      pid = $1; parent[pid] = $2; age[pid] = $3 + 0; comm[pid] = $4
      line = $0; sub(/^ *[^ ]+ +[^ ]+ +[^ ]+ +[^ ]+ +/, "", line); args[pid] = line
    }
    END {
      best = ""; best_age = -1; best_stamped = 1
      for (pid in comm) {
        if (comm[pid] != "codex" || index(args[pid], "app-server") > 0) continue
        p = pid
        while (p != "" && p != "0" && p != "1") {
          if (p in pane) {
            s = stamped[p]
            if (best == "" || (s < best_stamped) || (s == best_stamped && age[pid] < best_age)) {
              best = pane[p]; best_age = age[pid]; best_stamped = s
            }
            break
          }
          p = parent[p]
        }
      }
      if (best != "") print best
    }'
  return 0
}
