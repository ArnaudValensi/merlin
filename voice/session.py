"""Voice sessions: one per terminal window in voice mode.

A session follows its window's agent and turns what it does into control
frames for the pages (``{"type": "voice", "event": ..., "target": ...}``):

- it tails the agent's transcript (``voice/transcript.py``) for the user's
  prompt, the assistant's text and its tool calls, and the end of the turn;
- it watches ``@agent_state`` with a light poll for ``ask`` and for a turn that
  ends without a ``turn_duration`` line (Esc writes ``idle``);
- it makes the answer speakable, cuts it into pieces and synthesizes them in
  order through the TTS endpoint, at most a few ahead of what the page has
  fetched, into a small in-memory audio store the page reads by unguessable id.

Two clocks run side by side. The *watch* follows the transcript: a turn starts
with a prompt (or the agent going busy) and its end is queued when the
transcript says so. The *speaker* drains a FIFO of the turns' pieces and ends:
a turn whose generation is over may still have speech to deliver, and the next
turn's speech waits behind it, so nothing is dropped unless the user skips it.

Pages may come and go (a phone's socket drops in a car): the session keeps
running, waits for a page to fetch before synthesizing more, and ``snapshot()``
gives a reconnecting page its state back. A window that disappears stops its
session. ``@voice`` is set on start, cleared on stop, and cleared at server
startup for windows a previous process left marked.

Every tmux call and file read runs off the event loop.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from board import sweep

from . import speech, transcript, tts

logger = logging.getLogger("merlin.voice")

POLL_INTERVAL = 0.25
# A turn whose agent left busy without a turn_duration line (Esc, a crash) is
# over once the state has stayed out of busy this long.
END_GRACE = 2.0
# A prompt sent by voice that no turn picked up in this long is given up on.
PROMPT_TIMEOUT = 20.0
# Clips synthesized ahead of what the page has fetched. The speaker waits,
# however long, for a fetch (or for an unfetched clip to expire) before making
# another one: the bound holds while no page is connected.
MAX_AHEAD = 3
# How often a waiting speaker looks again for clips that expired unfetched.
ROOM_RECHECK = 5.0
# Pieces waiting to be synthesized, all turns together. Past it, the turn
# being written says the rest is in the terminal and drops what follows.
MAX_QUEUED = 64
# Clip records kept for a reconnecting page.
MAX_CLIPS = 24
EXCERPT_LINES = 8
# The ask sentence is a nicety: a slow voice server must not hold the watch.
ASK_DEADLINE = 5.0
# A conversation found after the session started (/clear, resume) is read from
# its start, without the lines stamped before it was seen minus this margin.
NEW_CONVERSATION_MARGIN = 2.0

PLAYABLE_FORMATS = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
}

ASK_SENTENCE = {
    "en": "The agent needs your answer.",
    "fr": "L'agent a besoin de ta réponse.",
}

REST_IN_TERMINAL = {
    "en": "The rest is in the terminal.",
    "fr": "La suite est dans le terminal.",
}

TOOL_LABELS = {
    "WebSearch": "web search",
    "WebFetch": "reading a page",
    "Bash": "command",
    "Read": "reading",
    "Grep": "searching",
    "Glob": "finding files",
    "Edit": "editing",
    "MultiEdit": "editing",
    "Write": "writing",
    "NotebookEdit": "editing",
    "Agent": "subagent",
    "Task": "subagent",
    "TodoWrite": "planning",
    "Skill": "skill",
}

_WINDOW_FIELDS = ("@agent_state", "@agent_provider", "@agent_transcript", "@agent_conv")
_TARGET_RE = re.compile(r"[^:\s]+:@\d+")
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b-\x1f\x7f]"
)


def tool_label(name: str) -> str:
    """A short label for a tool call (an MCP tool shows its own name)."""
    return TOOL_LABELS.get(name) or name.rsplit("__", 1)[-1] or "tool"


def valid_target(target: str) -> bool:
    """The dictation target rule: ``session:window_id``."""
    return bool(_TARGET_RE.fullmatch(target or ""))


def pane_excerpt(raw: str | None, lines: int = EXCERPT_LINES) -> str:
    """The bottom of a pane, without escapes or control characters and
    without trailing blank lines."""
    if not raw:
        return ""
    rows = [_ANSI.sub("", row).rstrip() for row in raw.splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows[-lines:])


# ---------------------------------------------------------------------------
# Audio store
# ---------------------------------------------------------------------------


@dataclass
class _Clip:
    data: bytes
    content_type: str
    created: float


class AudioStore:
    """Synthesized clips by unguessable id, bounded in bytes and in age."""

    def __init__(self, max_bytes: int = 32 * 1024 * 1024, ttl: float = 600.0):
        self.max_bytes = max_bytes
        self.ttl = ttl
        self._clips: OrderedDict[str, _Clip] = OrderedDict()
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._clips)

    def __contains__(self, clip_id: object) -> bool:
        self._evict()
        return clip_id in self._clips

    @property
    def total_bytes(self) -> int:
        return self._bytes

    def put(self, data: bytes, content_type: str) -> str:
        clip_id = secrets.token_urlsafe(18)
        self._clips[clip_id] = _Clip(data, content_type, time.monotonic())
        self._bytes += len(data)
        self._evict()
        return clip_id

    def get(self, clip_id: str) -> tuple[bytes, str] | None:
        self._evict()
        clip = self._clips.get(clip_id)
        return (clip.data, clip.content_type) if clip else None

    def drop(self, clip_ids) -> None:
        for clip_id in list(clip_ids):
            clip = self._clips.pop(clip_id, None)
            if clip:
                self._bytes -= len(clip.data)

    def _evict(self) -> None:
        now = time.monotonic()
        while self._clips:
            clip_id, clip = next(iter(self._clips.items()))
            if now - clip.created <= self.ttl and self._bytes <= self.max_bytes:
                break
            self._clips.pop(clip_id)
            self._bytes -= len(clip.data)


store = AudioStore()


# ---------------------------------------------------------------------------
# Frames and background tasks
# ---------------------------------------------------------------------------

Broadcast = Callable[[dict], Awaitable[None]]


async def _broadcast_to_pages(payload: dict) -> None:
    from terminal import routes as terminal_routes

    await terminal_routes.broadcast_control(payload)


# Tasks started without an owner to await them (a session stopping itself):
# held here so they are not garbage collected mid-run.
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class StartRefused(Exception):
    """Voice mode cannot start for this window; the message says why."""


_turn_ids = itertools.count(1)


@dataclass
class _Turn:
    id: int = field(default_factory=lambda: next(_turn_ids))
    started: float = field(default_factory=time.monotonic)
    saw_busy: bool = False
    idle_since: float | None = None
    # Generation is over: the end is queued behind the turn's pieces.
    end_queued: bool = False
    # The speaker reached the end: the turn is gone.
    finished: bool = False
    skipped: bool = False
    truncated: bool = False
    spoke: bool = False
    chunker: speech.Chunker = field(default_factory=speech.Chunker)


# Queued after a turn's last piece: (turn, _END).
_END = object()


class VoiceSession:
    def __init__(
        self,
        target: str,
        lang: str,
        *,
        config: tts.TtsConfig,
        provider: str,
        broadcast: Broadcast = _broadcast_to_pages,
        audio: AudioStore | None = None,
    ) -> None:
        self.target = target
        self.lang = lang
        self.config = config
        self.provider = provider
        self.broadcast = broadcast
        self.audio = audio if audio is not None else store
        self.state = "listening"
        self.heard = ""
        self.activity = ""
        self.ask: dict | None = None
        self.clips: list[dict] = []
        # The turn the transcript is writing, and every turn not yet fully
        # spoken, oldest first (the generating one is last when it exists).
        self.turn: _Turn | None = None
        self.live: list[_Turn] = []
        self.prompt_sent_at: float | None = None
        # Whether `busy` may start a turn: not right after a turn ended while
        # the state still said busy (turn_duration lands before the Stop hook
        # writes done), only once the state has been seen out of busy.
        self._busy_starts_turn = True
        self.reader: transcript.TranscriptReader | None = None
        self._reader_key: tuple[str, str] | None = None
        self._queue: deque[tuple[_Turn, object]] = deque()
        self._queued_pieces = 0
        self._queue_event = asyncio.Event()
        self._fetched: set[str] = set()
        self._room_event = asyncio.Event()
        self._ask_audio: dict[str, bytes] = {}
        self._tasks: list[asyncio.Task] = []
        # The in-flight @voice write: a cancelled start must let it land
        # before clearing it, or the clear could run first.
        self._marking: asyncio.Future | None = None
        self._stopped = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Mark the window, anchor the transcript, start the tasks. Anything
        failing or cancelled on the way rolls all of it back."""
        try:
            self._marking = asyncio.ensure_future(
                asyncio.to_thread(sweep.set_window_option, self.target, "@voice", "on")
            )
            if not await asyncio.shield(self._marking):
                raise StartRefused("Could not mark the window for voice mode")
            # Anchor the transcript now: everything written before voice mode
            # turned on is history, everything after is followed.
            window = await asyncio.to_thread(
                sweep.window_options, self.target, _WINDOW_FIELDS
            )
            if window is not None:
                await self._open_reader(window, initial=True)
            if self._stopped:
                raise StartRefused("Voice mode was turned off while starting")
            self._tasks = [
                asyncio.create_task(self._watch(), name=f"voice-watch {self.target}"),
                asyncio.create_task(self._speak(), name=f"voice-speak {self.target}"),
            ]
            await self._set_state("listening")
        except BaseException:
            await asyncio.shield(self._teardown())
            raise

    async def _teardown(self) -> None:
        """Cancel the tasks, clear the mark, drop the audio. Idempotent."""
        self._stopped = True
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        for task in self._tasks:
            if task is not current:
                try:
                    await task
                except BaseException:
                    pass
        if self._marking is not None:
            await asyncio.wait([self._marking])
        await asyncio.to_thread(sweep.unset_window_option, self.target, "@voice")
        self.audio.drop(c["id"] for c in self.clips)
        self.clips = []
        self._queue.clear()

    async def stop(self, reason: str = "") -> None:
        if self._stopped and not self._tasks:
            return
        await self._teardown()
        self._tasks = []
        if _sessions.get(self.target) is self:
            _sessions.pop(self.target, None)
        self.state = "off"
        await self._send("state", state="off", reason=reason)

    @property
    def stopped(self) -> bool:
        return self._stopped

    # -- frames --------------------------------------------------------------

    async def _send(self, event: str, **fields) -> None:
        try:
            await self.broadcast(
                {"type": "voice", "event": event, "target": self.target, **fields}
            )
        except Exception:
            logger.exception("voice frame failed: %s", event)

    async def _set_state(self, state: str) -> None:
        self.state = state
        await self._send("state", state=state)

    def snapshot(self) -> dict:
        """Everything a page that just (re)connected needs to pick up. Clips
        whose audio expired are left out."""
        return {
            "active": not self._stopped,
            "target": self.target,
            "lang": self.lang,
            "state": self.state,
            "heard": self.heard,
            "activity": self.activity,
            "ask": self.ask,
            "clips": [c for c in self.clips if c["id"] in self.audio],
            "turn": self.turn is not None,
        }

    # -- page actions -------------------------------------------------------

    def mark_fetched(self, clip_id: str) -> None:
        if any(c["id"] == clip_id for c in self.clips):
            self._fetched.add(clip_id)
            self._room_event.set()

    async def skip(self) -> None:
        """Drop the rest of the answer being heard (the oldest turn not yet
        fully spoken); listening resumes when that turn ends."""
        turn = next((t for t in self.live if not t.skipped), None)
        if turn is not None:
            self._drop_turn(turn)

    async def note_dictation(self, text: str | None, error: str | None) -> None:
        """The voice utterance this session's page uploaded was transcribed."""
        if error or not (text or "").strip():
            await self._send("error", message=error or "Nothing heard, try again")
            if self.turn is None:
                await self._set_state("listening")
            return
        self.prompt_sent_at = time.monotonic()
        if self.turn is None and self.state != "thinking":
            await self._set_state("thinking")

    def set_lang(self, lang: str) -> None:
        self.lang = lang

    # -- the watch loop -----------------------------------------------------

    async def _watch(self) -> None:
        try:
            while not self._stopped:
                window = await asyncio.to_thread(
                    sweep.window_options, self.target, _WINDOW_FIELDS
                )
                if window is None:
                    _spawn(stop_session(self.target, "The window was closed", self))
                    return
                await self._follow(window)
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("voice session failed: %s", self.target)
            await self._send("error", message="Voice mode failed, see Merlin logs")
            _spawn(stop_session(self.target, "Voice mode failed", self))

    async def _open_reader(self, window: dict[str, str], initial: bool = False) -> None:
        """Follow the window's current transcript.

        At start, from its end: what was said before is history. A
        conversation found later (``/clear``, resume, or the conversation-id
        fallback finding the file late) is read from its start, skipping the
        lines stamped before it was noticed, so its first turn is never lost
        and a resumed conversation's history is never read aloud."""
        key = (window["@agent_transcript"], window["@agent_conv"])
        if key == self._reader_key:
            return
        path = await asyncio.to_thread(transcript.find_transcript, self.provider, *key)
        if path is None:
            return
        if self.reader is None or self.reader.path != path:
            if initial:
                self.reader = await asyncio.to_thread(
                    transcript.open_reader, self.provider, path
                )
            else:
                since = datetime.now(UTC) - timedelta(
                    seconds=POLL_INTERVAL + NEW_CONVERSATION_MARGIN
                )
                self.reader = await asyncio.to_thread(
                    transcript.open_reader, self.provider, path, 0, since
                )
        self._reader_key = key

    async def _follow(self, window: dict[str, str]) -> None:
        await self._open_reader(window)
        if self.reader is not None:
            for event in await asyncio.to_thread(self.reader.poll):
                await self._on_event(event)
        await self._on_agent_state(window["@agent_state"])

    async def _begin_turn(self) -> _Turn:
        """The turn the transcript is writing, started if there is none. A
        turn whose end is queued is over for the transcript: what comes now
        belongs to the next one, while its speech keeps its place in line."""
        if self.turn is not None and not self.turn.end_queued:
            return self.turn
        turn = _Turn()
        self.turn = turn
        self.live.append(turn)
        self.prompt_sent_at = None
        self.activity = ""
        self.ask = None
        if len(self.live) == 1:
            await self._set_state("thinking")
        return turn

    async def _on_event(self, event: transcript.Event) -> None:
        if event.kind == "user_prompt":
            await self._begin_turn()
            self.heard = event.text
            await self._send("heard", text=event.text)
        elif event.kind == "tool_use":
            await self._begin_turn()
            self.activity = tool_label(event.text)
            await self._send("activity", label=self.activity)
        elif event.kind == "assistant_text":
            turn = await self._begin_turn()
            if turn.skipped or turn.truncated:
                return
            # A text block is complete when it lands: speak all of it now,
            # so the sentence said before a long tool run is heard before the
            # run, not after it.
            text = speech.speakable(event.text, self.lang)
            for piece in turn.chunker.feed(text + "\n") + turn.chunker.flush():
                self._enqueue(turn, piece)
        elif event.kind == "turn_end":
            await self._end_turn()

    def _enqueue(self, turn: _Turn, piece: str) -> None:
        if turn.truncated:
            return
        if self._queued_pieces >= MAX_QUEUED:
            # Nobody is fetching and the answer keeps coming: say where the
            # rest is (allowed past the bound, once per turn) and drop it.
            turn.truncated = True
            piece = REST_IN_TERMINAL.get(self.lang, REST_IN_TERMINAL["en"])
        self._queue.append((turn, piece))
        self._queued_pieces += 1
        self._queue_event.set()

    async def _end_turn(self) -> None:
        """Queue the end of the turn being written, once (a turn_duration line
        and the state fallback may both get here)."""
        turn = self.turn
        if turn is None or turn.end_queued:
            return
        turn.end_queued = True
        self._busy_starts_turn = False
        if not turn.skipped:
            for piece in turn.chunker.flush():
                self._enqueue(turn, piece)
        self._queue.append((turn, _END))
        self._queue_event.set()

    async def _on_agent_state(self, state: str) -> None:
        now = time.monotonic()
        if state == "ask":
            await self._begin_turn()
            if self.state != "ask":
                await self._enter_ask()
            return
        if self.state == "ask":
            self.ask = None
            speaking = any(t.spoke and not t.skipped for t in self.live)
            await self._set_state("speaking" if speaking else "thinking")
        if state == "busy":
            if self.turn is None or self.turn.end_queued:
                if not self._busy_starts_turn:
                    return
            turn = await self._begin_turn()
            turn.saw_busy = True
            turn.idle_since = None
            return
        self._busy_starts_turn = True
        turn = self.turn
        if turn is not None and not turn.end_queued:
            if turn.idle_since is None:
                turn.idle_since = now
            ended = turn.saw_busy and now - turn.idle_since >= END_GRACE
            # No hooks at all: never busy, so give it longer before ending.
            stale = not turn.saw_busy and now - turn.started >= PROMPT_TIMEOUT
            if ended or stale:
                await self._end_turn()
        elif (
            turn is None
            and self.prompt_sent_at
            and now - self.prompt_sent_at >= PROMPT_TIMEOUT
        ):
            self.prompt_sent_at = None
            await self._send("error", message="The agent did not take the prompt")
            await self._set_state("listening")

    async def _enter_ask(self) -> None:
        session_name, window_id = self.target.split(":", 1)
        raw = await asyncio.to_thread(sweep.capture_pane, session_name, window_id)
        audio_id = None
        sentence = ASK_SENTENCE.get(self.lang, ASK_SENTENCE["en"])
        data = self._ask_audio.get(self.lang)
        if data is None:
            try:
                data = await tts.synthesize(
                    sentence, self.lang, config=self.config, deadline=ASK_DEADLINE
                )
                self._ask_audio[self.lang] = data
            except tts.TtsError as e:
                logger.warning("voice ask sentence: %s", e)
        if data is not None:
            audio_id = self.audio.put(data, self._content_type())
        self.ask = {"excerpt": pane_excerpt(raw), "text": sentence, "audio": audio_id}
        self.state = "ask"
        await self._send("ask", **self.ask)
        await self._send("state", state="ask")

    # -- the speaker ----------------------------------------------------------

    def _content_type(self) -> str:
        return PLAYABLE_FORMATS.get(self.config.format, "application/octet-stream")

    def _drop_turn(self, turn: _Turn) -> None:
        """Skip one turn: its queued pieces and the clips the page has not
        fetched go; its end stays queued so the page hears the turn end."""
        turn.skipped = True
        kept: deque[tuple[_Turn, object]] = deque()
        for item in self._queue:
            if item[0] is turn and item[1] is not _END:
                self._queued_pieces -= 1
            else:
                kept.append(item)
        self._queue = kept
        dropped = [
            c["id"]
            for c in self.clips
            if c["turn"] == turn.id and c["id"] not in self._fetched
        ]
        self.audio.drop(dropped)
        self.clips = [c for c in self.clips if c["id"] not in dropped]
        self._room_event.set()
        self._queue_event.set()

    def _unfetched(self) -> int:
        return sum(
            1
            for c in self.clips
            if c["id"] not in self._fetched and c["id"] in self.audio
        )

    async def _wait_for_room(self, turn: _Turn) -> None:
        """Wait until fewer than MAX_AHEAD clips are waiting for the page, or
        the turn is skipped. No deadline: with no page, synthesis stops."""
        while not turn.skipped:
            # Clear before counting: a fetch landing after the count still
            # sets the event, so the wait below cannot miss it.
            self._room_event.clear()
            if self._unfetched() < MAX_AHEAD:
                return
            try:
                await asyncio.wait_for(self._room_event.wait(), ROOM_RECHECK)
            except TimeoutError:
                pass  # look again: an unfetched clip may have expired

    async def _next_item(self) -> tuple[_Turn, object]:
        while not self._queue:
            self._queue_event.clear()
            await self._queue_event.wait()
        turn, item = self._queue.popleft()
        if item is not _END:
            self._queued_pieces -= 1
        return turn, item

    def _prune_clips(self) -> None:
        """Keep the clip records a reconnecting page could still use."""
        live = {t.id for t in self.live}
        self.clips = [
            c
            for c in self.clips
            if c["id"] in self.audio
            and not (c["id"] in self._fetched and c["turn"] not in live)
        ]
        while len(self.clips) > MAX_CLIPS:
            oldest_fetched = next(
                (c for c in self.clips if c["id"] in self._fetched), self.clips[0]
            )
            self.clips.remove(oldest_fetched)
        self._fetched &= {c["id"] for c in self.clips}

    async def _finish_turn(self, turn: _Turn) -> None:
        turn.finished = True
        if turn in self.live:
            self.live.remove(turn)
        self._prune_clips()
        await self._send("turn_end", turn=turn.id)
        if self.turn is turn:
            self.turn = None
            self.activity = ""
        if self.state == "ask":
            return
        if not self.live:
            await self._set_state("listening")
        elif not any(t.spoke for t in self.live):
            await self._set_state("thinking")

    async def _speak(self) -> None:
        while True:
            turn, item = await self._next_item()
            if item is _END:
                await self._finish_turn(turn)
                continue
            if turn.skipped:
                continue
            await self._wait_for_room(turn)
            if turn.skipped:
                continue
            assert isinstance(item, str)
            try:
                data = await tts.synthesize(item, self.lang, config=self.config)
            except tts.TtsError as e:
                logger.warning("voice synthesis failed: %s", e)
                self._drop_turn(turn)
                await self._send("error", message=e.public)
                await self._set_state("error")
                continue
            if turn.skipped:
                continue
            clip = {
                "id": self.audio.put(data, self._content_type()),
                "text": item,
                "turn": turn.id,
            }
            self.clips.append(clip)
            self._prune_clips()
            await self._send("say", **clip)
            if not turn.spoke:
                turn.spoke = True
                if self.state != "ask":
                    await self._set_state("speaking")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_sessions: dict[str, VoiceSession] = {}
# One lock per window: a start and a stop for the same window never overlap,
# and a double tap gets the session the first tap made.
_locks: dict[str, asyncio.Lock] = {}


def _lock(target: str) -> asyncio.Lock:
    lock = _locks.get(target)
    if lock is None:
        lock = _locks[target] = asyncio.Lock()
    return lock


def get(target: str) -> VoiceSession | None:
    return _sessions.get(target)


async def start_session(
    target: str, lang: str, *, broadcast: Broadcast = _broadcast_to_pages
) -> VoiceSession:
    """Start voice mode on a window, or return its running session (with the
    language updated). Raises StartRefused with the reason it cannot run."""
    if not valid_target(target):
        raise StartRefused("Not a terminal window")
    async with _lock(target):
        existing = _sessions.get(target)
        if existing is not None and not existing.stopped:
            existing.set_lang(lang)
            return existing
        session = await _new_session(target, lang, broadcast)
        await session.start()  # rolls itself back on failure
        _sessions[target] = session
        return session


async def _new_session(target: str, lang: str, broadcast: Broadcast) -> VoiceSession:
    from lib import skills

    config = tts.tts_config()
    if config is None:
        raise StartRefused("No voice server configured (merlin config tts-url <url>)")
    if config.format not in PLAYABLE_FORMATS:
        raise StartRefused(
            f"The voice format {config.format!r} cannot be played by the browser"
        )
    window = await asyncio.to_thread(sweep.window_options, target, _WINDOW_FIELDS)
    if window is None:
        raise StartRefused("The window is gone")
    provider = window["@agent_provider"]
    if provider == "codex":
        raise StartRefused("Voice mode works with Claude Code only for now")
    if provider != "claude":
        raise StartRefused("No Claude Code session in this window")
    if not await asyncio.to_thread(skills.voice_hooks_installed):
        raise StartRefused(
            "Merlin's agent hooks are missing or out of date: "
            "merlin config agent-state-hooks auto, then restart Merlin and the agent"
        )
    return VoiceSession(
        target, lang, config=config, provider=provider, broadcast=broadcast
    )


async def stop_session(
    target: str, reason: str = "", only: VoiceSession | None = None
) -> bool:
    """Stop the window's session. ``only``: stop it only if it is still that
    session (a session stopping itself must not stop a newer one)."""
    async with _lock(target):
        session = _sessions.get(target)
        if only is not None and session is not only:
            if not only.stopped:
                await only.stop(reason)
            return False
        if session is None:
            # Nothing running here (a restart, or a stale page): still make
            # sure the window is not left marked.
            await asyncio.to_thread(sweep.unset_window_option, target, "@voice")
            return False
        await session.stop(reason)
        return True


async def stop_all() -> None:
    for target in list(_sessions):
        await stop_session(target, "Merlin is stopping")


def clear_stale_marks() -> list[str]:
    """Unset ``@voice`` on every window still marked (a previous process
    ended without stopping its sessions). Run at startup, off the loop."""
    cleared = []
    for target in sweep.windows_with_option("@voice"):
        if sweep.unset_window_option(target, "@voice"):
            cleared.append(target)
    return cleared


def find_clip(clip_id: str) -> tuple[bytes, str] | None:
    """A clip by id, marking it fetched for the session that made it."""
    found = store.get(clip_id)
    if found is not None:
        for session in _sessions.values():
            session.mark_fetched(clip_id)
    return found
