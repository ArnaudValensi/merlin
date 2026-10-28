"""Follow an agent's transcript and turn it into voice events.

The seam where providers plug in: ``open_reader(provider, path)`` returns a
reader whose ``poll()`` yields the new events since the last call. Only Claude
Code is implemented; a Codex reader would implement the same ``poll()``.

Claude Code writes ``~/.claude/projects/<slug>/<session_id>.jsonl``, one JSON
object per line. What matters here: the user's prompt (``type: user`` with
text), each completed assistant content block (``text``, ``tool_use``), and
``system/turn_duration``, written when a turn is complete. Subagent lines
(``isSidechain``), tool results, meta lines, slash-command records and every
other line type (``attachment``, where hook output lands, ``api-request``,
``mode``...) are ignored.

Reads are blocking file I/O: callers run ``poll()`` off the event loop.
"""

from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

EventKind = Literal["user_prompt", "assistant_text", "tool_use", "turn_end"]


@dataclass(frozen=True)
class Event:
    kind: EventKind
    text: str = ""


class TranscriptReader(Protocol):
    path: Path

    def poll(self) -> list[Event]: ...


class UnsupportedProvider(Exception):
    pass


# A poll reads at most this much; the rest waits for the next poll.
MAX_READ = 4 * 1024 * 1024
# Line ids remembered to never emit a block twice, bounded.
SEEN_LIMIT = 4096

# User lines that are Claude Code's own records, not something the user said.
_NOT_A_PROMPT = (
    "<command-",
    "<local-command-",
    "<task-notification",
    "<system-reminder",
    "<bash-",
    "[Request interrupted",
)


def _prompt_text(line: dict) -> str | None:
    """The user's prompt in a ``type: user`` line, or None for anything else
    (tool results, meta lines, compaction summaries, command records)."""
    if line.get("isMeta") or line.get("isCompactSummary") or "toolUseResult" in line:
        return None
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return None
    text = text.strip()
    if not text or text.startswith(_NOT_A_PROMPT):
        return None
    return text


class ClaudeTranscriptReader:
    """Tail one Claude Code transcript from a byte offset.

    ``offset=None`` starts at the current end of the file (or at 0 when it does
    not exist yet), so nothing said before is read again. A partial last line
    is kept until its newline arrives. A file replaced or truncated under the
    reader is read again from its start.
    """

    def __init__(self, path: Path | str, offset: int | None = None) -> None:
        self.path = Path(path)
        try:
            st = self.path.stat()
            self._inode: int | None = st.st_ino
            size = st.st_size
        except OSError:
            self._inode, size = None, 0
        self.offset = size if offset is None else offset
        self._partial = b""
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()

    def _remember(self, key: str) -> bool:
        """True the first time ``key`` is seen."""
        if key in self._seen:
            return False
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > SEEN_LIMIT:
            self._seen.discard(self._seen_order.popleft())
        return True

    def poll(self) -> list[Event]:
        try:
            st = self.path.stat()
        except OSError:
            return []
        if self._inode is not None and st.st_ino != self._inode:
            self.offset, self._partial = 0, b""
        elif st.st_size < self.offset:
            self.offset, self._partial = 0, b""
        self._inode = st.st_ino
        if st.st_size == self.offset:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read(MAX_READ)
        except OSError:
            return []
        self.offset += len(chunk)
        data = self._partial + chunk
        *lines, self._partial = data.split(b"\n")
        events: list[Event] = []
        for raw in lines:
            events.extend(self._parse(raw))
        return events

    def _parse(self, raw: bytes) -> list[Event]:
        try:
            line = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(line, dict) or line.get("isSidechain"):
            return []
        kind = line.get("type")
        uid = str(line.get("uuid") or "")
        if kind == "system" and line.get("subtype") == "turn_duration":
            return [Event("turn_end")]
        if kind == "user":
            text = _prompt_text(line)
            if text is None or (uid and not self._remember(uid)):
                return []
            return [Event("user_prompt", text)]
        if kind != "assistant":
            return []
        content = (line.get("message") or {}).get("content")
        if not isinstance(content, list):
            return []
        events: list[Event] = []
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text") or "")
                if text.strip() and self._remember(f"{uid}#{index}"):
                    events.append(Event("assistant_text", text))
            elif btype in ("tool_use", "server_tool_use"):
                key = str(block.get("id") or f"{uid}#{index}")
                if self._remember(key):
                    events.append(Event("tool_use", str(block.get("name") or "")))
        return events


def open_reader(
    provider: str, path: Path | str, offset: int | None = None
) -> TranscriptReader:
    """The transcript reader for an agent. Only ``claude`` exists for now."""
    if provider == "claude":
        return ClaudeTranscriptReader(path, offset)
    raise UnsupportedProvider(provider)


_CONV_RE = re.compile(r"[A-Za-z0-9_-]+")


def claude_projects_dir() -> Path:
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(custom).expanduser() if custom else Path.home() / ".claude"
    return base / "projects"


def find_transcript(provider: str, stamped: str, conv: str) -> Path | None:
    """Where the window's conversation is written.

    ``stamped`` is ``@agent_transcript`` (set by session-init from the hook's
    ``transcript_path``). A window whose agent started before that hook existed
    has only ``@agent_conv``: the conversation id is unique, so its file is
    found under any project directory. Blocking: run off the event loop.
    """
    if stamped and os.path.isabs(stamped):
        return Path(stamped)
    if provider != "claude" or not conv or not _CONV_RE.fullmatch(conv):
        return None
    for candidate in claude_projects_dir().glob(f"*/{conv}.jsonl"):
        return candidate
    return None
