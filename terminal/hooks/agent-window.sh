#!/bin/bash
# Shared by the agent hooks: the tmux window of the agent session this hook
# runs for. Sourced, defines agent_window. Prints the window id or nothing.
#
#   agent_window [session_id] [cwd] [event]
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
#   this conversation (@agent_conv, by the session init hook); at SessionStart
#   only, when no window carries it yet, the one pane in that directory that
#   runs a Codex TUI and has no conversation yet. Two such panes, or a later
#   event whose conversation no window carries, resolve to nothing: a hook
#   that cannot prove its window stamps none. A $TMUX whose socket is gone is
#   dropped first, so tmux reaches the default server.
#
# TMUX_PANE as given is the last resort for a hook with no payload at all;
# with a payload it is never reinstated once its ancestry check failed.

# Dropped at source time, in the hook's own shell, so every tmux call of the
# hook benefits (agent_window runs in a command substitution).
if [ -n "$TMUX" ] && [ ! -S "${TMUX%%,*}" ]; then
  unset TMUX
fi

agent_window() {
  local conv="$1" cwd="$2" event="$3" win="" p pid_line pane_pid pane_win
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
  if [ -z "$win" ] && [ -n "$cwd" ] && [ "$event" = "SessionStart" ]; then
    win=$(_agent_window_by_cwd "$cwd")
  fi
  if [ -z "$win" ] && [ -z "$conv" ] && [ -z "$cwd" ] && [ -n "$pane_win" ]; then
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

# The window of the one pane in directory $1 that runs a Codex TUI (a
# process named codex that is not the app-server daemon) and carries no
# conversation yet. Nothing when no pane qualifies, when two do (the binding
# would be a guess), or when ps cannot report processes.
_agent_window_by_cwd() {
  local cwd="$1" panes procs
  panes=$(tmux list-panes -a -F '#{pane_pid}	#{window_id}	#{@agent_conv}	#{pane_current_path}' 2>/dev/null) || return 0
  [ -n "$panes" ] || return 0
  procs=$(ps -eo pid=,ppid=,comm=,args= 2>/dev/null) || return 0
  printf '%s\n%s\n%s\n' "$panes" "---" "$procs" | awk -v cwd="$cwd" '
    BEGIN { section = "panes"; FS = "\t" }
    section == "panes" {
      if ($0 == "---") { section = "procs"; FS = " "; next }
      if ($4 == cwd && $3 == "") pane[$1] = $2
      next
    }
    {
      pid = $1; parent[pid] = $2; comm[pid] = $3
      line = $0; sub(/^ *[^ ]+ +[^ ]+ +[^ ]+ +/, "", line); args[pid] = line
    }
    END {
      found = ""; n = 0
      for (pid in comm) {
        if (comm[pid] != "codex" || index(args[pid], "app-server") > 0) continue
        p = pid
        while (p != "" && p != "0" && p != "1") {
          if (p in pane) {
            if (pane[p] != found) { n++; found = pane[p] }
            break
          }
          p = parent[p]
        }
      }
      if (n == 1) print found
    }'
  return 0
}
