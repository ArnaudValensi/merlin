"""What a notification says: the title, the body, the duration and the snippet.

Pure functions, no tmux and no delivery. The watcher gathers the facts (the
names, the state, how long the window was busy, the pane's last lines) and
``compose`` turns them into the ``title`` and ``body`` carried in the event,
so the page, the service worker and the future hub all show the same text
and none of them composes a second time.

Title: ``<window> · <session> · <environment>``, the most specific part first.
A missing window name reads ``window``, a missing environment is omitted with
its separator.

Body: the state, then what happened.

- ``Needs an answer: <snippet>`` for ``ask``
- ``Finished after <duration>: <snippet>`` for ``done`` with a known duration
- ``Finished: <snippet>`` for ``done`` without one
- the same three without the colon and the snippet when there is no snippet

The snippet is the tail of the agent's last message as read from its pane:
the input chrome (the prompt line and everything below it, rule lines, the
timing line) is stripped, then the last three non-empty lines are joined by
single spaces, whitespace collapsed, control characters removed, clipped to
240 characters with an ellipsis. The rules are tested against captured
fixtures of a Claude Code pane and a Codex pane.
"""

from __future__ import annotations

import re

SEPARATOR = " · "
SNIPPET_LINES = 3
SNIPPET_MAX = 240

# The input line of both agents: Claude Code's ``❯`` (also the cursor of its
# option dialogs, which leaves the question above it) and Codex's ``›``.
_PROMPT = re.compile(r"^\s*[❯›]")
# Rule and box lines start with a box-drawing character (``────``, ``╭``,
# ``│``, ``╰``): the input box borders, the welcome box, Codex's
# ``─ Worked for 1m 16s ───`` rule.
_RULE = re.compile(r"^\s*[─-╿]")
# Claude Code's timing line after a turn: ``✻ Cogitated for 1m 14s · done``.
_TIMING = re.compile(r"^\s*[^\w\s]{1,2}\s+\w+ for \d+")
# The first line of a message: ``●`` (Claude Code), ``•`` (Codex).
_BULLET = re.compile(r"^\s*[●•⏺]\s*")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def format_duration(seconds: float) -> str:
    """``40 s`` under a minute, ``14 min`` under an hour, ``1 h 20 min``
    beyond, ``3 h`` when the minutes round to nothing, ``12 h`` once the
    hours pass the single digit. Rounded, never negative."""
    seconds = max(0.0, float(seconds))
    whole = round(seconds)
    if whole < 60:
        return f"{whole} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours >= 10 or rest == 0:
        return f"{hours} h"
    return f"{hours} h {rest} min"


def compose_title(window_name: str, session: str, machine: str) -> str:
    parts = [window_name or "window", session]
    if machine:
        parts.append(machine)
    return SEPARATOR.join(parts)


def compose_body(state: str, busy_seconds: int | None, snippet: str) -> str:
    if state == "ask":
        lead = "Needs an answer"
    elif busy_seconds is not None:
        lead = f"Finished after {format_duration(busy_seconds)}"
    else:
        lead = "Finished"
    return f"{lead}: {snippet}" if snippet else lead


def compose(
    *,
    state: str,
    window_name: str,
    session: str,
    machine: str,
    busy_seconds: int | None,
    snippet: str,
) -> tuple[str, str]:
    """The ``(title, body)`` of one attention event."""
    return (
        compose_title(window_name, session, machine),
        compose_body(state, busy_seconds, snippet),
    )


def _is_chrome(line: str) -> bool:
    return not line.strip() or bool(_RULE.match(line)) or bool(_TIMING.match(line))


def clean_snippet(text: str | None) -> str:
    """The tail of the agent's last message from a pane capture, or an empty
    string when the capture holds nothing but chrome (or nothing at all)."""
    if not text:
        return ""
    text = _ANSI.sub("", text).replace(" ", " ").replace("\t", " ")
    text = _CONTROL.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    # The prompt line and everything below it go.
    for i in range(len(lines) - 1, -1, -1):
        if _PROMPT.match(lines[i]):
            del lines[i:]
            break
    # Then the chrome above the prompt: rules, the timing line, blank lines.
    while lines and _is_chrome(lines[-1]):
        lines.pop()
    # What remains ends with the last message. Walk back to its first line,
    # or three non-empty lines, whichever comes first.
    tail: list[str] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        if _is_chrome(line):
            break
        tail.append(line)
        if _BULLET.match(line) or len(tail) == SNIPPET_LINES:
            break
    tail.reverse()
    if tail:
        tail[0] = _BULLET.sub("", tail[0], count=1)
    joined = " ".join(" ".join(line.split()) for line in tail).strip()
    if len(joined) > SNIPPET_MAX:
        joined = joined[: SNIPPET_MAX - 1].rstrip() + "…"
    return joined
