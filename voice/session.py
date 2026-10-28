"""Voice sessions: one per terminal window in voice mode.

A session follows its window's agent and turns what it does into control
frames for the pages (``{"type": "voice", "event": ..., "target": ...}``):

- it tails the agent's transcript (``voice/transcript.py``) for the user's
  prompt, the assistant's text and its tool calls, and the end of the turn;
- it watches ``@agent_state`` with a light poll for ``ask`` and for a turn that
  ends without a ``turn_duration`` line (Esc writes ``idle``);
- it makes the answer speakable, cuts it into pieces and synthesizes them in
  order through the TTS endpoint, a few ahead of what the page has fetched,
  into a small in-memory audio store the page reads by unguessable id.

Pages may come and go (a phone's socket drops in a car): the session keeps
running and ``snapshot()`` gives a reconnecting page its state back. A window
that disappears stops its session. ``@voice`` is set on start, cleared on stop,
and cleared at server startup for windows a previous process left marked.

Every tmux call and file read runs off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from board import sweep

from . import speech, transcript, tts

logger = logging.getLogger("merlin.voice")

POLL_INTERVAL = 0.25
# A turn whose agent left busy without a turn_duration line (Esc, a crash) is
# over once the state has stayed out of busy this long.
END_GRACE = 2.0
# A prompt sent by voice that no turn picked up in this long is given up on.
PROMPT_TIMEOUT = 20.0
# Pieces synthesized ahead of what the page has fetched.
MAX_AHEAD = 3
# How long the speaker waits for the page to fetch before going on anyway
# (no page connected): the audio store bounds what piles up.
FETCH_WAIT = 30.0
EXCERPT_LINES = 8
# The ask sentence is a nicety: a slow voice server must not hold the watch.
ASK_DEADLINE = 5.0

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
# Frames
# ---------------------------------------------------------------------------

Broadcast = Callable[[dict], Awaitable[None]]

# Tasks started without an owner to await them (a session stopping itself):
# held here so they are not garbage collected mid-run.
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _broadcast_to_pages(payload: dict) -> None:
    from terminal import routes as terminal_routes

    await terminal_routes.broadcast_control(payload)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class StartRefused(Exception):
    """Voice mode cannot start for this window; the message says why."""


@dataclass
class _Turn:
    started: float = field(default_factory=time.monotonic)
    saw_busy: bool = False
    saw_prompt: bool = False
    idle_since: float | None = None
    skipped: bool = False
    spoke: bool = False
    end_queued: bool = False
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
        self.turn: _Turn | None = None
        self.prompt_sent_at: float | None = None
        self.reader: transcript.TranscriptReader | None = None
        self._reader_key: tuple[str, str] | None = None
        self._pieces: asyncio.Queue = asyncio.Queue()
        self._fetched: set[str] = set()
        self._fetch_event = asyncio.Event()
        self._ask_audio: dict[str, bytes] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopped = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if not await asyncio.to_thread(
            sweep.set_window_option, self.target, "@voice", "on"
        ):
            raise StartRefused("Could not mark the window for voice mode")
        # Anchor the transcript now: everything written before voice mode
        # turned on is history, everything after is followed.
        window = await asyncio.to_thread(
            sweep.window_options, self.target, _WINDOW_FIELDS
        )
        if window is not None:
            await self._open_reader(window)
        self._tasks = [
            asyncio.create_task(self._watch(), name=f"voice-watch {self.target}"),
            asyncio.create_task(self._speak(), name=f"voice-speak {self.target}"),
        ]
        await self._set_state("listening")

    async def stop(self, reason: str = "") -> None:
        if self._stopped:
            return
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
        await asyncio.to_thread(sweep.unset_window_option, self.target, "@voice")
        self.audio.drop(c["id"] for c in self.clips)
        self.clips = []
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
        """Everything a page that just (re)connected needs to pick up."""
        return {
            "active": not self._stopped,
            "target": self.target,
            "lang": self.lang,
            "state": self.state,
            "heard": self.heard,
            "activity": self.activity,
            "ask": self.ask,
            "clips": list(self.clips),
            "turn": self.turn is not None,
        }

    # -- page actions -------------------------------------------------------

    def mark_fetched(self, clip_id: str) -> None:
        if any(c["id"] == clip_id for c in self.clips):
            self._fetched.add(clip_id)
            self._fetch_event.set()

    async def skip(self) -> None:
        """Drop the rest of this answer; listening resumes when the turn ends."""
        if self.turn is None:
            return
        self.turn.skipped = True
        self._drain_pieces()
        unplayed = [c["id"] for c in self.clips if c["id"] not in self._fetched]
        self.audio.drop(unplayed)
        self.clips = [c for c in self.clips if c["id"] in self._fetched]
        self._fetch_event.set()

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
                    _spawn(self.stop("The window was closed"))
                    return
                await self._follow(window)
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("voice session failed: %s", self.target)
            await self._send("error", message="Voice mode failed, see Merlin logs")
            _spawn(self.stop("Voice mode failed"))

    async def _open_reader(self, window: dict[str, str]) -> None:
        """Follow the window's current transcript. A new one (``/clear``,
        resume) is followed from its end as it is now, or from its start when
        it does not exist yet."""
        key = (window["@agent_transcript"], window["@agent_conv"])
        if key == self._reader_key:
            return
        path = await asyncio.to_thread(transcript.find_transcript, self.provider, *key)
        if path is None:
            return
        if self.reader is None or self.reader.path != path:
            self.reader = await asyncio.to_thread(
                transcript.open_reader, self.provider, path
            )
        self._reader_key = key

    async def _follow(self, window: dict[str, str]) -> None:
        await self._open_reader(window)
        if self.reader is not None:
            for event in await asyncio.to_thread(self.reader.poll):
                await self._on_event(event)
        await self._on_agent_state(window["@agent_state"])

    async def _begin_turn(self) -> None:
        # A turn whose end is already queued is over: whatever comes now
        # belongs to the next one.
        if self.turn is not None and not self.turn.end_queued:
            return
        self.turn = _Turn()
        self.prompt_sent_at = None
        self.activity = ""
        self.ask = None
        self.audio.drop(c["id"] for c in self.clips)
        self.clips = []
        self._fetched.clear()
        await self._set_state("thinking")

    async def _on_event(self, event: transcript.Event) -> None:
        if event.kind == "user_prompt":
            await self._begin_turn()
            assert self.turn is not None
            self.turn.saw_prompt = True
            self.heard = event.text
            await self._send("heard", text=event.text)
        elif event.kind == "tool_use":
            await self._begin_turn()
            self.activity = tool_label(event.text)
            await self._send("activity", label=self.activity)
        elif event.kind == "assistant_text":
            await self._begin_turn()
            assert self.turn is not None
            if self.turn.skipped:
                return
            # A text block is complete when it lands: speak all of it now,
            # so the sentence said before a long tool run is heard before the
            # run, not after it.
            chunker = self.turn.chunker
            text = speech.speakable(event.text, self.lang)
            for piece in chunker.feed(text + "\n") + chunker.flush():
                self._pieces.put_nowait((self.turn, piece))
        elif event.kind == "turn_end":
            await self._end_turn()

    async def _end_turn(self) -> None:
        """Queue the end of the current turn, once (a turn_duration line and
        the state fallback may both get here)."""
        turn = self.turn
        if turn is None or turn.end_queued:
            return
        turn.end_queued = True
        if not turn.skipped:
            for piece in turn.chunker.flush():
                self._pieces.put_nowait((turn, piece))
        self._pieces.put_nowait((turn, _END))

    async def _on_agent_state(self, state: str) -> None:
        now = time.monotonic()
        turn = self.turn
        if state == "ask":
            if turn is None:
                await self._begin_turn()
            if self.state != "ask":
                await self._enter_ask()
            return
        if self.state == "ask":
            self.ask = None
            spoke = self.turn is not None and self.turn.spoke
            await self._set_state("speaking" if spoke else "thinking")
        if state == "busy":
            if turn is None:
                await self._begin_turn()
                turn = self.turn
            assert turn is not None
            turn.saw_busy = True
            turn.idle_since = None
            return
        if turn is not None:
            if turn.idle_since is None:
                turn.idle_since = now
            ended = turn.saw_busy and now - turn.idle_since >= END_GRACE
            # No hooks at all: never busy, so give it longer before ending.
            stale = not turn.saw_busy and now - turn.started >= PROMPT_TIMEOUT
            if ended or stale:
                await self._end_turn()
        elif self.prompt_sent_at and now - self.prompt_sent_at >= PROMPT_TIMEOUT:
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

    def _drain_pieces(self) -> None:
        """Drop queued pieces, keeping the turn ends."""
        kept = []
        while not self._pieces.empty():
            item = self._pieces.get_nowait()
            if item[1] is _END:
                kept.append(item)
        for item in kept:
            self._pieces.put_nowait(item)

    async def _wait_for_room(self) -> None:
        deadline = time.monotonic() + FETCH_WAIT
        while time.monotonic() < deadline:
            # Clear before counting: a fetch landing after the count still
            # sets the event, so the wait below cannot miss it.
            self._fetch_event.clear()
            ahead = sum(1 for c in self.clips if c["id"] not in self._fetched)
            if ahead < MAX_AHEAD:
                return
            try:
                await asyncio.wait_for(
                    self._fetch_event.wait(), deadline - time.monotonic()
                )
            except TimeoutError:
                break

    async def _speak(self) -> None:
        while True:
            turn, item = await self._pieces.get()
            if item is _END:
                if self.turn is turn:
                    self.turn = None
                    self.activity = ""
                    await self._send("turn_end")
                    await self._set_state("listening")
                continue
            if turn.skipped or self.turn is not turn:
                continue
            await self._wait_for_room()
            if turn.skipped or self.turn is not turn:
                continue
            try:
                data = await tts.synthesize(item, self.lang, config=self.config)
            except tts.TtsError as e:
                logger.warning("voice synthesis failed: %s", e)
                turn.skipped = True
                self._drain_pieces()
                await self._send("error", message=str(e))
                await self._set_state("error")
                continue
            if turn.skipped or self.turn is not turn:
                continue
            clip = {"id": self.audio.put(data, self._content_type()), "text": item}
            self.clips.append(clip)
            await self._send("say", **clip)
            if not turn.spoke:
                turn.spoke = True
                if self.state != "ask":
                    await self._set_state("speaking")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_sessions: dict[str, VoiceSession] = {}
# Targets with a start in flight: a double tap must not make two sessions.
_starting: set[str] = set()


def get(target: str) -> VoiceSession | None:
    return _sessions.get(target)


async def start_session(
    target: str, lang: str, *, broadcast: Broadcast = _broadcast_to_pages
) -> VoiceSession:
    """Start voice mode on a window, or return its running session (with the
    language updated). Raises StartRefused with the reason it cannot run."""
    if not valid_target(target):
        raise StartRefused("Not a terminal window")
    existing = _sessions.get(target)
    if existing is not None and not existing.stopped:
        existing.set_lang(lang)
        return existing
    if target in _starting:
        raise StartRefused("Voice mode is already starting for this window")
    _starting.add(target)
    try:
        return await _start_new(target, lang, broadcast)
    finally:
        _starting.discard(target)


async def _start_new(target: str, lang: str, broadcast: Broadcast) -> VoiceSession:
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
    session = VoiceSession(
        target, lang, config=config, provider=provider, broadcast=broadcast
    )
    _sessions[target] = session
    try:
        await session.start()
    except BaseException:
        _sessions.pop(target, None)
        raise
    return session


async def stop_session(target: str, reason: str = "") -> bool:
    session = _sessions.get(target)
    if session is None:
        # Nothing running here (a restart, or a stale page): still make sure
        # the window is not left marked.
        await asyncio.to_thread(sweep.unset_window_option, target, "@voice")
        return False
    await session.stop(reason)
    return True


async def stop_all() -> None:
    for session in list(_sessions.values()):
        await session.stop("Merlin is stopping")


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
