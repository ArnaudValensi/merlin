#!/bin/bash
# UserPromptSubmit companion for voice mode (see voice/). When the tmux window of
# the current pane has @voice = on, print the voice instructions on stdout:
# Claude Code adds a UserPromptSubmit hook's stdout to the agent's context, so
# the agent answers for the ear without anything prefixed to what the user said.
# Prints nothing otherwise. Always exits 0 and never waits on anything but one
# tmux read, so it can never block or fail a prompt.

# Drain stdin (the hook JSON is not needed) so the writer never blocks.
cat >/dev/null 2>&1 || true

command -v tmux >/dev/null 2>&1 || exit 0

# The window of this pane: $TMUX_PANE, or the ancestor walk when a helper
# process stripped the environment (see agent-window.sh).
. "$(dirname "$0")/agent-window.sh"
win=$(agent_window)
[ -n "$win" ] || exit 0

[ "$(tmux show-option -wqv -t "$win" @voice 2>/dev/null)" = "on" ] || exit 0

cat <<'EOF'
Voice mode is on: the user is talking to you by voice, from a phone, often driving or walking. Their message was transcribed from speech, and the prose of your answer will be read aloud by a speech synthesizer.
- Answer in the language of the question.
- Keep the spoken prose short and direct: two to four sentences, unless the user asks for more.
- Write for the ear: no markdown, no bullet lists, no tables, no emoji, no URLs in the prose.
- Write numbers, units and symbols the way they are pronounced.
- The transcription may get words wrong: guess what was meant rather than pointing it out.
- Do the work itself as usual: tools, edits and code are all fine. Code stays in the terminal and is not read aloud, so do not spell it out in the prose.
- Before a long tool run, say in one short sentence what you are about to do.
EOF
exit 0
