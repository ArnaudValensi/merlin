#!/bin/bash
# Set the agent state on the tmux window of the current pane, for the
# status-bar pill (see terminal/tmux.conf window-status-format). Arg $1 = state:
#   idle  -> ○  (grey)     no turn running / waiting on nothing
#   busy  -> ◐  (amber)    the agent is working
#   ask   -> ?  (sky)      blocked on you: a dialog is open mid-turn
#   done  -> ●  (green)    finished, waiting on you
# No-op outside tmux or if tmux is unavailable, so it can never block a session.
#
# Agent-neutral: it only writes @agent_state. The Claude Code and Codex
# drivers wire it through their user hook files (installed by lib/skills.py):
#   UserPromptSubmit -> busy   Stop -> done   SessionStart -> idle
#   blocking question / permission -> ask   resolved tool -> busy
# An interrupt (Esc mid-turn) fires no hook at all in Claude Code, so
# terminal/tmux.conf binds Esc to write idle on a busy/ask window itself.

# Read the hook JSON (the writer must never block on a pipe nobody drains).
# Its session id and cwd identify the window when the hook runs outside the
# pane, in Codex's app-server daemon (see agent-window.sh); the regexes need
# no jq. A cwd JSON had to escape is skipped rather than half decoded.
payload=$(cat 2>/dev/null) || payload=""
conv=""
re='"session_id"[[:space:]]*:[[:space:]]*"([A-Za-z0-9_-]+)"'
if [[ $payload =~ $re ]]; then
  conv="${BASH_REMATCH[1]}"
fi
cwd=""
re='"cwd"[[:space:]]*:[[:space:]]*"(/[^"\\]*)"'
if [[ $payload =~ $re ]]; then
  cwd="${BASH_REMATCH[1]}"
fi
event=""
re='"hook_event_name"[[:space:]]*:[[:space:]]*"([A-Za-z]+)"'
if [[ $payload =~ $re ]]; then
  event="${BASH_REMATCH[1]}"
fi
# A job run's first prompt names its job: "[Job: id]" or
# "[Triggered by webhook: id]" (the prompt Merlin wrote, see job/runs.py).
job=""
re='"prompt"[[:space:]]*:[[:space:]]*"\[(Job|Triggered by webhook): ([a-z0-9-]+)\]'
if [[ $payload =~ $re ]]; then
  job="${BASH_REMATCH[2]}"
fi

state="${1:-idle}"
command -v tmux >/dev/null 2>&1 || exit 0

# The window of this agent: $TMUX_PANE when this hook descends from the
# pane, the ancestor walk when a helper process stripped the environment,
# the conversation's own evidence when a daemon runs the hook (see
# agent-window.sh).
. "$(dirname "$0")/agent-window.sh"
win=$(agent_window "$conv" "$cwd" "$event" "$job")
[ -n "$win" ] || exit 0

# A window bound here for the first time (a job's first prompt, resolved by
# its job id) takes the conversation, so every later event finds it.
if [ -n "$conv" ] && [ -z "$(tmux show-option -wqv -t "$win" @agent_conv 2>/dev/null)" ]; then
  tmux set-option -w -t "$win" @agent_conv "$conv" 2>/dev/null
fi
tmux set-option -w -t "$win" @agent_state "$state" 2>/dev/null
# A job's window whose run has finished, and whose agent works again: someone
# gave it a follow-up, and retention must keep the window (job/retention.py).
if [ "$state" = busy ] && [ -n "$(tmux show-option -wqv -t "$win" @job_finished 2>/dev/null)" ]; then
  tmux set-option -w -t "$win" @job_stepped_in 1 2>/dev/null
fi
tmux refresh-client -S 2>/dev/null
exit 0
