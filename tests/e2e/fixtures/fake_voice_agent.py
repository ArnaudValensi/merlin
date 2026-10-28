"""A stand-in for Claude Code in a tmux window, for the voice console e2e.

It stamps the window options the agent hooks would (``@agent_provider``,
``@agent_transcript``, ``@agent_state``), then answers each line typed into
the pane by writing a turn into its transcript the way Claude Code does: the
user line, a text block, a tool call, the final text, ``turn_duration``. With
``ask`` in the control file it blocks on a dialog first and reads one raw key:
Enter prints ``ANSWER:YES``, Escape ``ANSWER:NO``.

Usage: python3 fake_voice_agent.py <transcript.jsonl> <control-file>
"""

import datetime
import json
import os
import subprocess
import sys
import termios
import time
import tty
import uuid

transcript, control = sys.argv[1], sys.argv[2]
window = subprocess.run(
    ["tmux", "display-message", "-p", "-t", os.environ["TMUX_PANE"], "#{window_id}"],
    capture_output=True,
    text=True,
).stdout.strip()
session_id = str(uuid.uuid4())


def option(name, value):
    subprocess.run(["tmux", "set-option", "-w", "-t", window, name, value])


def now():
    return datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")


def write(line):
    line.update(
        {
            "uuid": str(uuid.uuid4()),
            "timestamp": now(),
            "sessionId": session_id,
            "isSidechain": False,
        }
    )
    with open(transcript, "a") as f:
        f.write(json.dumps(line) + "\n")


def assistant(block, stop):
    write(
        {
            "type": "assistant",
            "message": {
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "role": "assistant",
                "content": [block],
                "stop_reason": stop,
            },
        }
    )


def read_key():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        return os.read(fd, 1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


open(transcript, "a").close()
option("@agent_provider", "claude")
option("@agent_transcript", transcript)
option("@agent_state", "idle")
print("FAKE AGENT READY", flush=True)

for line in sys.stdin:
    prompt = line.strip()
    if not prompt:
        continue
    mode = open(control).read().strip() if os.path.exists(control) else "answer"
    option("@agent_state", "busy")
    write({"type": "user", "message": {"role": "user", "content": prompt}})
    time.sleep(0.3)
    assistant({"type": "text", "text": "Running the **tests** now."}, "tool_use")
    assistant(
        {
            "type": "tool_use",
            "id": "toolu_e2e",
            "name": "Bash",
            "input": {"command": "pytest"},
        },
        "tool_use",
    )
    time.sleep(1.0)
    if mode == "ask":
        print("Allow Bash: pytest?  Enter = yes, Esc = no", flush=True)
        option("@agent_state", "ask")
        key = read_key()
        answer = {b"\r": "YES", b"\n": "YES", b"\x1b": "NO"}.get(key, repr(key))
        print("ANSWER:" + answer, flush=True)
        option("@agent_state", "busy")
    assistant(
        {"type": "text", "text": "All 42 tests pass. Nothing else to do."}, "end_turn"
    )
    write({"type": "system", "subtype": "turn_duration", "durationMs": 1500})
    option("@agent_state", "done")
