#!/bin/bash
# Shared by the agent hooks: the tmux window of the agent session this hook
# runs for. Sourced, defines agent_window. Prints the window id or nothing.
#
#   agent_window [session_id] [cwd] [event] [job_id]
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
#   and no ancestor is a pane. Nothing in the hook's reach names the session's
#   process (the daemon holds the per-thread locks itself, the session
#   metadata records no pid, nothing in the daemon's environment names the
#   session), so the hook binds only on what is tied to the conversation:
#     1. the window already stamped with the payload's session id
#        (@agent_conv, by the session init hook or by 2);
#     2. at the first prompt of a job run (the prompt Merlin itself wrote,
#        "[Job: id]" or "[Triggered by webhook: id]"), the one window that
#        carries that @job_id in that cwd and no conversation yet, which then
#        takes the conversation.
#   A SessionStart the daemon runs for a session no window carries binds
#   nothing: the pane's directory and the time it started are not proof that
#   it emitted the event, and losing a binding is better than overwriting
#   another conversation's identity. So a Codex session a person opens in a
#   tmux window under the daemon gets no pills until Codex names its process.
#   A $TMUX whose socket is gone is dropped first, so tmux reaches the
#   default server.
#
# TMUX_PANE as given is the last resort for a hook with no payload at all;
# with a payload it is never reinstated once its ancestry check failed.

# Dropped at source time, in the hook's own shell, so every tmux call of the
# hook benefits (agent_window runs in a command substitution).
if [ -n "$TMUX" ] && [ ! -S "${TMUX%%,*}" ]; then
  unset TMUX
fi

agent_window() {
  local conv="$1" cwd="$2" event="$3" job="$4" win="" p pid_line pane_pid pane_win
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
    win=$(tmux list-windows -a -F '#{window_id}	#{@agent_conv}' 2>/dev/null \
      | awk -F'\t' -v c="$conv" '$2 == c { print $1; exit }')
  fi
  if [ -z "$win" ] && [ -n "$cwd" ] && [ -n "$job" ] && [ "$event" = "UserPromptSubmit" ]; then
    win=$(_agent_window_by_job "$job" "$cwd")
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

# The one window carrying @job_id $1 in directory $2 (its @agent_cwd) with
# no conversation yet: a job run whose agent sent its first prompt.
_agent_window_by_job() {
  local job="$1" cwd="$2"
  tmux list-windows -a -F '#{window_id}	#{@job_id}	#{@agent_conv}	#{@agent_cwd}' 2>/dev/null \
    | awk -F'\t' -v j="$job" -v c="$cwd" '$2 == j && $3 == "" && $4 == c { n++; w = $1 } END { if (n == 1) print w }'
  return 0
}
