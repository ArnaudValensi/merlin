"""voice/session.py and the voice routes.

The integration tests run real sessions against a private tmux server (bare
``tmux`` reaches it through TMUX_TMPDIR, the way board.sweep calls it), a fake
agent (the test writes the transcript lines and sets ``@agent_state``, the way
Claude Code and its hooks do) and a fake OpenAI-compatible TTS server (the
reference server's HTTP layer with a fake synthesizer). The frames a page would
receive are collected from the session's broadcast.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from board import sweep
from lib import skills
from voice import routes, session
from voice.session import AudioStore, VoiceSession

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

SERVER_PATH = Path(__file__).parents[2] / "tools" / "kokoro-tts-server.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("kokoro_tts_server_s", SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server_mod = _load_server()


class FakeSynth:
    """Returns the text itself as the 'audio', so tests can read what was said."""

    voices = ["af_heart", "ff_siwis"]
    provider = "Fake"

    def __init__(self):
        self.calls = []

    def synthesize(self, text, voice, speed):
        self.calls.append((text, voice))
        data = text.encode()
        return data + b"\0" * (len(data) % 2)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tts_server(monkeypatch):
    synth = FakeSynth()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.make_handler(synth))
    threading.Thread(
        target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    host, port = srv.server_address[:2]
    monkeypatch.setenv("MERLIN_TTS_URL", f"http://{host}:{port}/v1")
    monkeypatch.setenv("MERLIN_TTS_FORMAT", "pcm")
    # pcm is the format whose bytes the tests can read back; the page needs a
    # playable one, so start_session refuses pcm and the tests bypass that.
    yield srv, synth
    srv.shutdown()
    srv.server_close()


class Tmux:
    """A private tmux server that bare `tmux` (board.sweep) reaches."""

    def __init__(self, root: Path):
        self.root = root
        self.socket = root / f"tmux-{os.getuid()}" / "default"

    def __call__(self, *args) -> str:
        return subprocess.run(
            ["tmux", "-S", str(self.socket), *args],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.rstrip("\n")

    def wopt(self, win, name):
        return self("show-option", "-wqv", "-t", win, name)

    def set(self, win, name, value):
        self("set-option", "-w", "-t", win, name, value)


@pytest.fixture
def tmux(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="vt", dir="/tmp"))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(root))
    t = Tmux(root)
    t.socket.parent.mkdir(mode=0o700)
    subprocess.run(
        [
            "tmux",
            "-S",
            str(t.socket),
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "vs",
        ],
        check=True,
        env={**os.environ, "TMUX_TMPDIR": str(root)},
    )
    # Bare tmux must land on the private server, never on a real one.
    probe = subprocess.run(
        ["tmux", "display-message", "-p", "#{socket_path}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert probe == str(t.socket), probe
    yield t
    t("kill-server")
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def window(tmux, tmp_path):
    """A Claude Code window: provider stamped, transcript path stamped."""
    win = tmux("new-window", "-d", "-t", "vs:", "-P", "-F", "#{window_id}")
    path = tmp_path / "conv.jsonl"
    path.write_text("")
    tmux.set(win, "@agent_provider", "claude")
    tmux.set(win, "@agent_transcript", str(path))
    tmux.set(win, "@agent_state", "idle")
    return f"vs:{win}", win, path


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(session, "POLL_INTERVAL", 0.02)
    monkeypatch.setattr(session, "END_GRACE", 0.3)
    monkeypatch.setattr(session, "PROMPT_TIMEOUT", 1.0)
    monkeypatch.setattr(session, "ROOM_RECHECK", 0.05)
    monkeypatch.setattr(skills, "voice_hooks_installed", lambda: True)
    monkeypatch.setattr(session, "store", AudioStore())
    monkeypatch.setattr(session, "_sessions", {})
    monkeypatch.setattr(session, "_locks", {})


class Pages:
    """Collects the frames a page would get."""

    def __init__(self):
        self.frames: list[dict] = []

    async def __call__(self, payload):
        self.frames.append(payload)

    def events(self, *kinds):
        return [f for f in self.frames if not kinds or f["event"] in kinds]

    def states(self):
        return [f["state"] for f in self.frames if f["event"] == "state"]

    async def wait(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out; frames: {json.dumps(self.frames)[:2000]}")


def line(**fields):
    return json.dumps(fields) + "\n"


def prompt(text, uid):
    return line(type="user", uuid=uid, message={"role": "user", "content": text})


def say(blocks, uid):
    return line(type="assistant", uuid=uid, message={"content": blocks})


TURN_END = line(type="system", subtype="turn_duration", durationMs=1)


def session_start(tmux, win, path):
    """What session-init does at SessionStart: the mark, then the path."""
    size = path.stat().st_size if path.exists() else 0
    tmux.set(win, "@agent_transcript_mark", f"{size} {int(time.time())}")
    tmux.set(win, "@agent_transcript", str(path))


def append(path, *chunks):
    with open(path, "a") as f:
        f.write("".join(chunks))


async def _start(target, pages, lang="en"):
    # pcm (readable bytes in tests) is not a playable format: allow it here.
    session.PLAYABLE_FORMATS.setdefault("pcm", "audio/pcm")
    try:
        return await session.start_session(target, lang, broadcast=pages)
    finally:
        session.PLAYABLE_FORMATS.pop("pcm", None)


def _fetch_all(running):
    """What a page does: fetch every clip it was told about."""
    return [session.find_clip(c["id"]) for c in running.clips]


# ---------------------------------------------------------------------------
# Start refusals
# ---------------------------------------------------------------------------


class TestStart:
    def test_refusals_have_reasons(self, tmux, window, tts_server, fast, monkeypatch):
        target, win, _ = window
        pages = Pages()

        def refused(t, **env):
            for key, value in env.items():
                monkeypatch.setenv(key, value)

            async def go():
                with pytest.raises(session.StartRefused) as exc:
                    await session.start_session(t, "en", broadcast=pages)
                return str(exc.value)

            return asyncio.run(go())

        assert "Not a terminal window" in refused("nonsense")
        assert "cannot be played" in refused(target)  # pcm
        monkeypatch.setenv("MERLIN_TTS_FORMAT", "wav")
        assert "gone" in refused("vs:@999")
        tmux.set(win, "@agent_provider", "codex")
        assert "Claude Code only" in refused(target)
        tmux("set-option", "-w", "-u", "-t", win, "@agent_provider")
        assert "No Claude Code session" in refused(target)
        tmux.set(win, "@agent_provider", "claude")
        monkeypatch.setattr(skills, "voice_hooks_installed", lambda: False)
        assert "hooks" in refused(target)
        monkeypatch.delenv("MERLIN_TTS_URL")
        assert "No voice server" in refused(target)
        assert tmux.wopt(win, "@voice") == ""
        assert session._sessions == {}

    def test_a_double_tap_makes_one_session(self, tmux, window, tts_server, fast):
        target, _, _ = window
        pages = Pages()

        async def go():
            first, second = await asyncio.gather(
                _start(target, pages), _start(target, pages)
            )
            assert isinstance(first, VoiceSession) and first is second
            assert session.get(target) is first
            await session.stop_session(target)

        asyncio.run(go())

    def test_start_marks_the_window_and_stop_clears_it(
        self, tmux, window, tts_server, fast
    ):
        target, win, _ = window
        pages = Pages()

        async def go():
            running = await _start(target, pages)
            assert tmux.wopt(win, "@voice") == "on"
            assert session.get(target) is running
            again = await _start(target, pages, lang="fr")
            assert again is running and running.lang == "fr"
            await session.stop_session(target, "bye")
            assert session.get(target) is None
            assert all(t.done() for t in running._tasks)

        asyncio.run(go())
        assert tmux.wopt(win, "@voice") == ""
        assert pages.states()[0] == "listening"
        assert pages.frames[-1] == {
            "type": "voice",
            "event": "state",
            "target": target,
            "state": "off",
            "reason": "bye",
        }


# ---------------------------------------------------------------------------
# A whole turn
# ---------------------------------------------------------------------------


class TestTurn:
    def test_a_turn_from_prompt_to_listening(self, tmux, window, tts_server, fast):
        target, win, path = window
        _, synth = tts_server
        append(
            path,
            prompt("an old question", "o1"),
            say([{"type": "text", "text": "Old answer."}], "o2"),
        )
        pages = Pages()

        async def go():
            running = await _start(target, pages)
            tmux.set(win, "@agent_state", "busy")
            append(path, prompt("what is the build status", "u1"))
            await pages.wait(lambda p: p.events("heard"))
            append(
                path,
                say(
                    [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
                    "a1",
                ),
                say(
                    [
                        {
                            "type": "text",
                            "text": "The **build** passed on `main`. Here is the log:\n"
                            "```\nok 42 tests\n```\nNothing else to do.",
                        }
                    ],
                    "a2",
                ),
                line(
                    type="attachment",
                    attachment={"type": "hook_success", "content": "Voice mode is on"},
                ),
            )
            await pages.wait(lambda p: len(p.events("say")) >= 3)
            fetched = _fetch_all(running)
            append(path, TURN_END)
            tmux.set(win, "@agent_state", "done")
            await pages.wait(lambda p: p.events("turn_end"))
            fetched += _fetch_all(running)[len(fetched) :]
            await session.stop_session(target)
            return fetched

        fetched = asyncio.run(go())
        events = [f["event"] for f in pages.frames]
        assert events.index("heard") < events.index("activity") < events.index("say")
        assert pages.events("heard")[0]["text"] == "what is the build status"
        assert pages.events("activity")[0]["label"] == "command"
        spoken = [f["text"] for f in pages.events("say")]
        assert spoken == [
            "The build passed on main.",
            "Here is the log: The code is in the terminal.",
            "Nothing else to do.",
        ]
        assert [t for t, _ in synth.calls] == spoken
        assert all(v == "af_heart" for _, v in synth.calls)
        # Audio ids are unique, unguessable, served in order, with the bytes.
        ids = [f["id"] for f in pages.events("say")]
        assert len(set(ids)) == len(ids) and all(len(i) >= 24 for i in ids)
        assert [d.rstrip(b"\0").decode() for d, _ in fetched] == spoken
        states = pages.states()
        assert states[:3] == ["listening", "thinking", "speaking"]
        assert states[-2:] == ["listening", "off"]
        assert events.index("turn_end") > max(
            i for i, e in enumerate(events) if e == "say"
        )
        assert "Old answer" not in json.dumps(pages.frames)
        assert "Voice mode is on" not in json.dumps(pages.frames)

    def test_the_sentence_before_a_tool_run_is_said_during_it(
        self, tmux, window, tts_server, fast
    ):
        target, win, path = window
        pages = Pages()

        async def go():
            await _start(target, pages)
            tmux.set(win, "@agent_state", "busy")
            append(
                path,
                prompt("run the tests", "u1"),
                say(
                    [
                        {"type": "text", "text": "Running the tests."},
                        {"type": "tool_use", "id": "t", "name": "Bash"},
                    ],
                    "a1",
                ),
            )
            # The agent is still busy with the tool: no turn end yet.
            await pages.wait(lambda p: p.events("say"))
            await session.stop_session(target)

        asyncio.run(go())
        assert pages.events("say")[0]["text"] == "Running the tests."
        assert pages.events("turn_end") == []

    def test_turn_ends_when_the_agent_leaves_busy(self, tmux, window, tts_server, fast):
        target, win, path = window
        pages = Pages()

        async def go():
            await _start(target, pages)
            tmux.set(win, "@agent_state", "busy")
            append(path, prompt("hello", "u1"))
            await pages.wait(lambda p: "thinking" in p.states())
            tmux.set(win, "@agent_state", "idle")  # Esc: no turn_duration line
            await pages.wait(lambda p: p.events("turn_end"))
            await session.stop_session(target)

        asyncio.run(go())
        assert pages.states()[-2:] == ["listening", "off"]

    def test_french_voice_and_code_phrase(self, tmux, window, tts_server, fast):
        target, win, path = window
        _, synth = tts_server
        pages = Pages()

        async def go():
            await _start(target, pages, lang="fr")
            tmux.set(win, "@agent_state", "busy")
            append(
                path,
                prompt("salut", "u1"),
                say([{"type": "text", "text": "```\nx\n```"}], "a1"),
                TURN_END,
            )
            await pages.wait(lambda p: p.events("turn_end"))
            await session.stop_session(target)

        asyncio.run(go())
        assert synth.calls == [("Le code est dans le terminal.", "ff_siwis")]

    def test_an_empty_answer_goes_back_to_listening(
        self, tmux, window, tts_server, fast
    ):
        target, win, path = window
        pages = Pages()

        async def go():
            await _start(target, pages)
            append(
                path,
                prompt("do it", "u1"),
                say([{"type": "tool_use", "id": "t", "name": "Edit"}], "a1"),
                TURN_END,
            )
            await pages.wait(lambda p: p.events("turn_end"))
            await session.stop_session(target)

        asyncio.run(go())
        assert pages.events("say") == []
        assert pages.states()[-2:] == ["listening", "off"]

    def test_prefetch_is_bounded_by_what_the_page_fetched(
        self, tmux, window, tts_server, fast
    ):
        target, win, path = window
        pages = Pages()
        sentences = " ".join(
            f"This is sentence number {i} of the answer." for i in range(8)
        )

        async def go():
            running = await _start(target, pages)
            tmux.set(win, "@agent_state", "busy")
            append(
                path,
                prompt("long", "u1"),
                say([{"type": "text", "text": sentences}], "a1"),
            )
            await pages.wait(lambda p: len(p.events("say")) >= session.MAX_AHEAD)
            await asyncio.sleep(0.3)
            held = len(pages.events("say"))
            session.find_clip(running.clips[0]["id"])
            await pages.wait(lambda p: len(p.events("say")) > held)
            await session.stop_session(target)
            return held

        assert asyncio.run(go()) == session.MAX_AHEAD

    def test_skip_drops_the_rest_and_waits_for_the_turn_end(
        self, tmux, window, tts_server, fast
    ):
        target, win, path = window
        pages = Pages()
        sentences = " ".join(
            f"This is sentence number {i} of the answer." for i in range(8)
        )

        async def go():
            running = await _start(target, pages)
            tmux.set(win, "@agent_state", "busy")
            append(
                path,
                prompt("long", "u1"),
                say([{"type": "text", "text": sentences}], "a1"),
            )
            await pages.wait(lambda p: len(p.events("say")) >= 1)
            session.find_clip(running.clips[0]["id"])
            await running.skip()
            count = len(pages.events("say"))
            append(
                path,
                say(
                    [{"type": "text", "text": "More words that nobody hears now."}],
                    "a2",
                ),
            )
            await asyncio.sleep(0.3)
            assert len(pages.events("say")) == count
            assert [c["id"] for c in running.clips] == [pages.events("say")[0]["id"]]
            append(path, TURN_END)
            await pages.wait(lambda p: p.events("turn_end"))
            await session.stop_session(target)

        asyncio.run(go())

    def test_tts_down_mid_answer(self, tmux, window, tts_server, fast):
        target, win, path = window
        srv, _ = tts_server
        pages = Pages()

        async def go():
            running = await _start(target, pages)
            tmux.set(win, "@agent_state", "busy")
            append(
                path,
                prompt("q", "u1"),
                say([{"type": "text", "text": "First part is fine."}], "a1"),
            )
            await pages.wait(lambda p: p.events("say"))
            _fetch_all(running)
            srv.shutdown()
            srv.server_close()
            append(
                path,
                say([{"type": "text", "text": "This part cannot be said."}], "a2"),
                TURN_END,
            )
            await pages.wait(lambda p: p.events("turn_end"))
            await session.stop_session(target)

        asyncio.run(go())
        assert "unreachable" in pages.events("error")[0]["message"]
        states = pages.states()
        assert states.index("error") < len(states) - 2
        assert states[-2:] == ["listening", "off"]

    def test_a_new_transcript_after_clear_is_followed(
        self, tmux, window, tts_server, fast, tmp_path
    ):
        target, win, path = window
        pages = Pages()
        fresh = tmp_path / "after-clear.jsonl"

        async def go():
            await _start(target, pages)
            await asyncio.sleep(0.1)
            session_start(tmux, win, fresh)  # SessionStart after /clear
            await asyncio.sleep(0.1)
            append(
                fresh,
                prompt("new conversation", "u1"),
                say([{"type": "text", "text": "Fresh start."}], "a1"),
                TURN_END,
            )
            await pages.wait(lambda p: p.events("turn_end"))
            await session.stop_session(target)

        asyncio.run(go())
        assert pages.events("heard")[0]["text"] == "new conversation"
        assert [f["text"] for f in pages.events("say")] == ["Fresh start."]

    def test_transcript_found_from_the_conversation_id(
        self, tmux, window, tts_server, fast, tmp_path, monkeypatch
    ):
        # A window whose agent started before session-init stamped the path.
        target, win, _ = window
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
        conv = tmp_path / "cc" / "projects" / "-p" / "c0nv.jsonl"
        conv.parent.mkdir(parents=True)
        conv.write_text("")
        tmux("set-option", "-w", "-u", "-t", win, "@agent_transcript")
        tmux.set(win, "@agent_conv", "c0nv")
        pages = Pages()

        async def go():
            await _start(target, pages)
            await asyncio.sleep(0.1)
            append(conv, prompt("via conv", "u1"), TURN_END)
            await pages.wait(lambda p: p.events("turn_end"))
            await session.stop_session(target)

        asyncio.run(go())
        assert pages.events("heard")[0]["text"] == "via conv"


# ---------------------------------------------------------------------------
# Ask, dictation, window life
# ---------------------------------------------------------------------------


class TestAskAndLife:
    def test_ask_shows_the_bottom_of_the_pane(self, tmux, window, tts_server, fast):
        target, win, path = window
        _, synth = tts_server
        pages = Pages()
        tmux(
            "send-keys",
            "-t",
            win,
            "printf 'Allow Bash?\\n> 1. Yes\\n  2. No\\n'",
            "Enter",
        )

        async def go():
            await _start(target, pages)
            tmux.set(win, "@agent_state", "busy")
            append(path, prompt("run it", "u1"))
            await pages.wait(lambda p: "thinking" in p.states())
            tmux.set(win, "@agent_state", "ask")
            await pages.wait(lambda p: p.events("ask"))
            tmux.set(win, "@agent_state", "busy")
            await pages.wait(lambda p: p.states()[-1] == "thinking")
            tmux.set(win, "@agent_state", "ask")  # a second dialog
            await pages.wait(lambda p: len(p.events("ask")) == 2)
            await session.stop_session(target)

        asyncio.run(go())
        # The ask sentence is synthesized once, then reused.
        assert [t for t, _ in synth.calls].count("The agent needs your answer.") == 1
        ask = pages.events("ask")[0]
        assert "Allow Bash?" in ask["excerpt"] and "2. No" in ask["excerpt"]
        assert ask["text"] == "The agent needs your answer."
        assert ask["audio"]
        assert pages.states()[:4] == ["listening", "thinking", "ask", "thinking"]

    def test_nothing_heard(self, tmux, window, tts_server, fast):
        target, _, _ = window
        pages = Pages()

        async def go():
            running = await _start(target, pages)
            await running.note_dictation("", None)
            await running.note_dictation(None, "Transcription refused")
            await session.stop_session(target)

        asyncio.run(go())
        assert [f["message"] for f in pages.events("error")] == [
            "Nothing heard, try again",
            "Transcription refused",
        ]

    def test_a_prompt_nobody_took_is_given_up(self, tmux, window, tts_server, fast):
        target, _, _ = window
        pages = Pages()

        async def go():
            running = await _start(target, pages)
            await running.note_dictation("hello?", None)
            await pages.wait(lambda p: p.events("error"), timeout=3)
            await session.stop_session(target)

        asyncio.run(go())
        assert (
            pages.events("error")[0]["message"] == "The agent did not take the prompt"
        )
        assert pages.states()[:3] == ["listening", "thinking", "listening"]

    def test_a_vanished_window_stops_its_session(self, tmux, window, tts_server, fast):
        target, win, _ = window
        pages = Pages()

        async def go():
            running = await _start(target, pages)
            tmux("kill-window", "-t", win)
            await pages.wait(lambda p: "off" in p.states())
            await asyncio.sleep(0.05)
            return running

        running = asyncio.run(go())
        assert running.stopped and session.get(target) is None
        assert pages.frames[-1]["reason"] == "The window was closed"

    def test_reconnect_gets_the_state_back(self, tmux, window, tts_server, fast):
        target, win, path = window
        pages = Pages()

        async def go():
            await _start(target, pages)
            tmux.set(win, "@agent_state", "busy")
            append(
                path,
                prompt("status?", "u1"),
                say(
                    [
                        {"type": "tool_use", "id": "t", "name": "Grep"},
                        {"type": "text", "text": "All good here, nothing broke."},
                    ],
                    "a1",
                ),
            )
            await pages.wait(lambda p: p.events("say"))
            snap = session.get(target).snapshot()
            await session.stop_session(target)
            return snap

        snap = asyncio.run(go())
        assert snap["active"] and snap["turn"]
        assert snap["state"] == "speaking"
        assert snap["heard"] == "status?"
        assert snap["activity"] == "searching"
        assert [c["text"] for c in snap["clips"]] == ["All good here, nothing broke."]

    def test_stale_marks_are_cleared_at_startup(self, tmux, window):
        _, win, _ = window
        other = tmux("new-window", "-d", "-t", "vs:", "-P", "-F", "#{window_id}")
        tmux.set(win, "@voice", "on")
        assert session.clear_stale_marks() == [f"vs:{win}"]
        assert tmux.wopt(win, "@voice") == "" and tmux.wopt(other, "@voice") == ""

    def test_options_never_land_on_another_window(self, tmux, window):
        _, win, _ = window
        assert not sweep.set_window_option("vs:@999", "@voice", "on")
        assert sweep.window_options("vs:@999", ("@voice",)) is None
        current = tmux("display-message", "-p", "-t", "vs:", "#{window_id}")
        assert tmux.wopt(current, "@voice") == ""


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


class TestPieces:
    def test_audio_store_is_bounded(self):
        s = AudioStore(max_bytes=10, ttl=60)
        a = s.put(b"12345", "audio/wav")
        b = s.put(b"67890", "audio/wav")
        assert s.get(a) and s.get(b)
        c = s.put(b"x", "audio/wav")
        assert s.get(a) is None and s.get(b) and s.get(c)
        assert s.total_bytes == 6

    def test_audio_store_expires(self, monkeypatch):
        s = AudioStore(ttl=10)
        now = [1000.0]
        monkeypatch.setattr(session.time, "monotonic", lambda: now[0])
        a = s.put(b"abc", "audio/wav")
        now[0] += 11
        assert s.get(a) is None and len(s) == 0 and s.total_bytes == 0

    def test_audio_store_drop(self):
        s = AudioStore()
        a = s.put(b"abc", "audio/wav")
        s.drop([a, "unknown"])
        assert s.get(a) is None and s.total_bytes == 0

    def test_pane_excerpt(self):
        raw = "a\n\x1b[1;32mgreen\x1b[0m\x07 bell\n" + "\n" * 5
        assert session.pane_excerpt(raw) == "a\ngreen bell"
        assert (
            session.pane_excerpt("\n".join(str(i) for i in range(20)), 3)
            == "17\n18\n19"
        )
        assert session.pane_excerpt(None) == ""

    def test_tool_labels(self):
        assert session.tool_label("Bash") == "command"
        assert session.tool_label("mcp__github__create_issue") == "create_issue"
        assert session.tool_label("") == "tool"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class TestRoutes:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setattr(session, "_sessions", {})
        app = FastAPI()
        app.include_router(routes.api_router, prefix="/api/voice")
        return TestClient(app)

    def _fake_session(self, monkeypatch, target="s:@1"):
        calls = []

        class Fake:
            lang = "en"

            async def skip(self):
                calls.append("skip")

            def set_lang(self, lang):
                self.lang = lang

            def snapshot(self):
                return {"active": True, "target": target}

        session._sessions[target] = Fake()  # type: ignore[assignment]
        monkeypatch.setattr(
            sweep, "send_enter", lambda t: calls.append(("enter", t)) or True
        )
        monkeypatch.setattr(
            sweep, "send_escape", lambda t: calls.append(("escape", t)) or True
        )
        monkeypatch.setattr(
            sweep, "window_options", lambda t, n: {"@agent_state": "ask"}
        )
        monkeypatch.setattr(
            sweep,
            "set_window_option",
            lambda t, n, v: calls.append(("set", t, n, v)) or True,
        )
        return calls

    def test_yes_is_enter_and_no_is_escape(self, client, monkeypatch):
        calls = self._fake_session(monkeypatch)
        assert client.post(
            "/api/voice/answer", json={"target": "s:@1", "yes": True}
        ).json() == {"ok": True}
        assert client.post(
            "/api/voice/answer", json={"target": "s:@1", "yes": False}
        ).json() == {"ok": True}
        assert calls == [
            ("enter", "s:@1"),
            ("escape", "s:@1"),
            ("set", "s:@1", "@agent_state", "idle"),
        ]

    def test_answer_only_reaches_a_voice_window(self, client, monkeypatch):
        calls = self._fake_session(monkeypatch)
        r = client.post("/api/voice/answer", json={"target": "s:@2", "yes": True})
        assert r.status_code == 404 and calls == []

    def test_interrupt_skips_and_escapes(self, client, monkeypatch):
        calls = self._fake_session(monkeypatch)
        assert client.post("/api/voice/interrupt", json={"target": "s:@1"}).json() == {
            "ok": True
        }
        assert calls[:2] == ["skip", ("escape", "s:@1")]

    def test_session_and_lang(self, client, monkeypatch):
        self._fake_session(monkeypatch)
        assert client.get("/api/voice/session", params={"target": "s:@1"}).json()[
            "active"
        ]
        assert client.get("/api/voice/session", params={"target": "s:@9"}).json() == {
            "active": False,
            "target": "s:@9",
        }
        assert (
            client.get("/api/voice/session", params={"target": "bad"}).status_code
            == 400
        )
        assert (
            client.post(
                "/api/voice/lang", json={"target": "s:@1", "lang": "FR"}
            ).json()["lang"]
            == "fr"
        )
        assert (
            client.post(
                "/api/voice/lang", json={"target": "s:@1", "lang": "xx"}
            ).json()["lang"]
            == "en"
        )

    def test_start_refusal_is_a_409_with_the_reason(self, client, monkeypatch):
        monkeypatch.delenv("MERLIN_TTS_URL", raising=False)
        r = client.post("/api/voice/start", json={"target": "s:@1", "lang": "en"})
        assert r.status_code == 409 and "No voice server" in r.json()["error"]

    def test_audio_by_id(self, client, monkeypatch):
        monkeypatch.setattr(session, "store", AudioStore())
        clip = session.store.put(b"RIFFdata", "audio/wav")
        r = client.get(f"/api/voice/audio/{clip}")
        assert r.status_code == 200 and r.content == b"RIFFdata"
        assert r.headers["content-type"] == "audio/wav"
        assert client.get("/api/voice/audio/nope").status_code == 404

    def test_stop_without_a_session_still_clears_the_mark(self, client, monkeypatch):
        cleared = []
        monkeypatch.setattr(
            sweep, "unset_window_option", lambda t, n: cleared.append((t, n)) or True
        )
        assert client.post("/api/voice/stop", json={"target": "s:@3"}).json() == {
            "ok": True
        }
        assert cleared == [("s:@3", "@voice")]


class TestDictationNote:
    def test_a_voice_window_hears_how_its_utterance_went(self, monkeypatch):
        from terminal import routes as terminal_routes

        notes = []

        class Fake:
            async def note_dictation(self, text, error):
                notes.append((text, error))

        monkeypatch.setattr(session, "_sessions", {"s:@1": Fake()})
        asyncio.run(terminal_routes._note_voice("s:@1", "hello", None))
        asyncio.run(terminal_routes._note_voice("s:@1", None, "refused"))
        # A window without voice mode: nothing happens, nothing fails.
        asyncio.run(terminal_routes._note_voice("s:@2", "hello", None))
        assert notes == [("hello", None), (None, "refused")]


# ---------------------------------------------------------------------------
# Review m2-004: conversation anchor, turn ownership, backpressure,
# lifecycle, browser-safe errors, page isolation
# ---------------------------------------------------------------------------


def stamped(fields, when):
    return json.dumps({**fields, "timestamp": when}) + "\n"


def now_iso(offset=0.0):
    from datetime import UTC, datetime, timedelta

    return (
        (datetime.now(UTC) + timedelta(seconds=offset))
        .isoformat()
        .replace("+00:00", "Z")
    )


class TestNewConversation:
    def test_first_turn_already_written_when_the_new_path_is_seen(
        self, tmux, window, tts_server, fast, tmp_path
    ):
        target, win, _ = window
        pages = Pages()
        fresh = tmp_path / "cleared.jsonl"

        async def go():
            running = await _start(target, pages)
            # Pause the watch so the whole first turn lands before it looks.
            running._tasks[0].cancel()
            session_start(tmux, win, fresh)  # /clear: the file is not there yet
            t = now_iso()
            append(
                fresh,
                stamped(
                    {
                        "type": "user",
                        "uuid": "u1",
                        "message": {"content": "first after clear"},
                    },
                    t,
                ),
                stamped(
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "content": [{"type": "text", "text": "Answer after clear."}]
                        },
                    },
                    t,
                ),
                stamped({"type": "system", "subtype": "turn_duration"}, t),
            )
            await asyncio.sleep(3.0)  # however late the watch looks
            window_now = sweep.window_options(target, session._WINDOW_FIELDS)
            await running._follow(window_now)
            await pages.wait(lambda p: p.events("turn_end"))
            await session.stop_session(target)

        asyncio.run(go())
        assert pages.events("heard")[0]["text"] == "first after clear"
        assert [f["text"] for f in pages.events("say")] == ["Answer after clear."]

    def test_a_resumed_conversation_does_not_replay_its_history(
        self, tmux, window, tts_server, fast, tmp_path
    ):
        target, win, _ = window
        pages = Pages()
        old = tmp_path / "resumed.jsonl"
        hist = now_iso(-1)  # recent history, inside any fixed lookback
        append(
            old,
            stamped(
                {"type": "user", "uuid": "h1", "message": {"content": "old question"}},
                hist,
            ),
            stamped(
                {
                    "type": "assistant",
                    "uuid": "h2",
                    "message": {"content": [{"type": "text", "text": "Old answer."}]},
                },
                hist,
            ),
            stamped({"type": "system", "subtype": "turn_duration"}, hist),
        )

        async def go():
            await _start(target, pages)
            session_start(tmux, win, old)  # claude --resume
            await asyncio.sleep(0.1)
            t = now_iso()
            append(
                old,
                stamped(
                    {
                        "type": "user",
                        "uuid": "n1",
                        "message": {"content": "new question"},
                    },
                    t,
                ),
                stamped(
                    {
                        "type": "assistant",
                        "uuid": "n2",
                        "message": {
                            "content": [{"type": "text", "text": "New answer."}]
                        },
                    },
                    t,
                ),
                stamped({"type": "system", "subtype": "turn_duration"}, t),
            )
            await pages.wait(lambda p: p.events("turn_end"))
            await session.stop_session(target)

        asyncio.run(go())
        assert [f["text"] for f in pages.events("heard")] == ["new question"]
        assert [f["text"] for f in pages.events("say")] == ["New answer."]

    def test_fallback_finds_the_file_late_with_its_first_turn(
        self, tmux, window, tts_server, fast, tmp_path, monkeypatch
    ):
        target, win, _ = window
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
        tmux("set-option", "-w", "-u", "-t", win, "@agent_transcript")
        tmux.set(win, "@agent_conv", "late-conv")
        pages = Pages()

        async def go():
            await _start(target, pages)
            await asyncio.sleep(0.1)  # the file does not exist yet
            conv = tmp_path / "cc" / "projects" / "-p" / "late-conv.jsonl"
            conv.parent.mkdir(parents=True)
            append(
                conv,
                prompt("found late", "u1"),
                say([{"type": "text", "text": "Still heard."}], "a1"),
                TURN_END,
            )
            await pages.wait(lambda p: p.events("turn_end"))
            await session.stop_session(target)

        asyncio.run(go())
        assert [f["text"] for f in pages.events("say")] == ["Still heard."]


class SlowSynth:
    """Replaces tts.synthesize: the first call waits for a release."""

    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()
        self.fail_on = set()

    async def __call__(self, text, lang, *, config=None, client=None, deadline=None):
        self.calls.append(text)
        if len(self.calls) == 1:
            await self.release.wait()
        if text in self.fail_on:
            from voice import tts

            raise tts.TtsUnreachable("down", "The voice server is unreachable")
        return text.encode()


class TestTurnOwnership:
    def test_two_turns_in_one_poll_are_both_spoken(
        self, tmux, window, tts_server, fast
    ):
        target, win, path = window
        pages = Pages()

        async def go():
            running = await _start(target, pages)
            running._tasks[0].cancel()  # drive the watch by hand: one poll
            append(
                path,
                prompt("one", "u1"),
                say([{"type": "text", "text": "First answer."}], "a1"),
                TURN_END,
                prompt("two", "u2"),
                say([{"type": "text", "text": "Second answer."}], "a2"),
                TURN_END,
            )
            await running._follow(sweep.window_options(target, session._WINDOW_FIELDS))
            await pages.wait(lambda p: len(p.events("turn_end")) == 2)
            await session.stop_session(target)

        asyncio.run(go())
        assert [f["text"] for f in pages.events("say")] == [
            "First answer.",
            "Second answer.",
        ]
        ends = [f["turn"] for f in pages.events("turn_end")]
        assert len(set(ends)) == 2
        assert pages.states()[-2:] == ["listening", "off"]

    def test_turn_two_arrives_while_turn_one_is_synthesizing(
        self, tmux, window, tts_server, fast, monkeypatch
    ):
        target, win, path = window
        pages = Pages()
        slow = SlowSynth()
        monkeypatch.setattr(session.tts, "synthesize", slow)

        async def go():
            await _start(target, pages)
            append(
                path,
                prompt("one", "u1"),
                say([{"type": "text", "text": "First answer."}], "a1"),
                TURN_END,
            )
            while not slow.calls:
                await asyncio.sleep(0.02)
            append(
                path,
                prompt("two", "u2"),
                say([{"type": "text", "text": "Second answer."}], "a2"),
                TURN_END,
            )
            await asyncio.sleep(0.2)
            slow.release.set()
            await pages.wait(lambda p: len(p.events("turn_end")) == 2)
            await session.stop_session(target)

        asyncio.run(go())
        assert [f["text"] for f in pages.events("say")] == [
            "First answer.",
            "Second answer.",
        ]

    def test_an_old_turns_failure_keeps_the_new_turns_speech(
        self, tmux, window, tts_server, fast, monkeypatch
    ):
        target, win, path = window
        pages = Pages()
        slow = SlowSynth()
        slow.fail_on = {"First answer."}
        monkeypatch.setattr(session.tts, "synthesize", slow)

        async def go():
            await _start(target, pages)
            append(
                path,
                prompt("one", "u1"),
                say([{"type": "text", "text": "First answer."}], "a1"),
                TURN_END,
            )
            while not slow.calls:
                await asyncio.sleep(0.02)
            append(
                path,
                prompt("two", "u2"),
                say([{"type": "text", "text": "Second answer."}], "a2"),
                TURN_END,
            )
            await asyncio.sleep(0.2)
            slow.release.set()
            await pages.wait(lambda p: len(p.events("turn_end")) == 2)
            await session.stop_session(target)

        asyncio.run(go())
        assert [f["text"] for f in pages.events("say")] == ["Second answer."]
        assert pages.events("error")[0]["message"] == "The voice server is unreachable"

    def test_skip_is_scoped_to_the_answer_being_heard(
        self, tmux, window, tts_server, fast, monkeypatch
    ):
        target, win, path = window
        pages = Pages()
        slow = SlowSynth()
        monkeypatch.setattr(session.tts, "synthesize", slow)

        async def go():
            running = await _start(target, pages)
            append(
                path,
                prompt("one", "u1"),
                say([{"type": "text", "text": "First answer."}], "a1"),
                TURN_END,
            )
            while not slow.calls:
                await asyncio.sleep(0.02)
            append(
                path,
                prompt("two", "u2"),
                say([{"type": "text", "text": "Second answer."}], "a2"),
            )
            await asyncio.sleep(0.2)
            await running.skip()
            slow.release.set()
            append(path, TURN_END)
            await pages.wait(lambda p: len(p.events("turn_end")) == 2)
            await session.stop_session(target)

        asyncio.run(go())
        assert [f["text"] for f in pages.events("say")] == ["Second answer."]


class TestBackpressure:
    LONG = " ".join(f"This is sentence number {i} of a long answer." for i in range(12))

    def test_the_bound_holds_long_after_the_page_stopped_fetching(
        self, tmux, window, tts_server, fast
    ):
        target, win, path = window
        pages = Pages()

        async def go():
            await _start(target, pages)
            append(
                path,
                prompt("long", "u1"),
                say([{"type": "text", "text": self.LONG}], "a1"),
            )
            await pages.wait(lambda p: len(p.events("say")) >= session.MAX_AHEAD)
            await asyncio.sleep(10 * session.ROOM_RECHECK)
            count = len(pages.events("say"))
            await session.stop_session(target)
            return count

        assert asyncio.run(go()) == session.MAX_AHEAD

    def test_one_huge_text_block_is_cut_with_a_notice(
        self, tmux, window, tts_server, fast, monkeypatch
    ):
        target, win, path = window
        monkeypatch.setattr(session, "HARD_QUEUED", 6)
        pages = Pages()

        async def go():
            running = await _start(target, pages)
            append(path, say([{"type": "text", "text": self.LONG * 5}], "a1"))
            await asyncio.sleep(0.3)
            queued = list(running._queue)
            await session.stop_session(target)
            return queued

        texts = [item for _, item in asyncio.run(go()) if isinstance(item, str)]
        assert len(texts) <= 7
        assert texts[-1] == "The rest is in the terminal."

    def test_many_turns_behind_a_blocked_speaker_stay_bounded_then_all_play(
        self, tmux, window, tts_server, fast, monkeypatch
    ):
        target, win, path = window
        monkeypatch.setattr(session, "MAX_QUEUED", 4)
        monkeypatch.setattr(session, "MAX_LIVE", 3)
        monkeypatch.setattr(session, "POLL_EVENTS", 4)
        pages = Pages()
        n = 40

        async def go():
            running = await _start(target, pages)
            for i in range(n):
                append(
                    path,
                    prompt(f"q{i}", f"u{i}"),
                    say([{"type": "text", "text": f"Answer {i}."}], f"a{i}"),
                    TURN_END,
                )
            peak = {"queue": 0, "live": 0, "clips": 0}
            for _ in range(40):  # the page is away: nothing is fetched
                await asyncio.sleep(0.02)
                peak["queue"] = max(peak["queue"], len(running._queue))
                peak["live"] = max(peak["live"], len(running.live))
                peak["clips"] = max(peak["clips"], len(running.clips))
            assert len(pages.events("say")) == session.MAX_AHEAD
            # Stop and skip stay responsive while backlogged.
            await running.skip()
            # The page comes back and fetches everything as it arrives.
            deadline = time.monotonic() + 20
            while len(pages.events("turn_end")) < n and time.monotonic() < deadline:
                for c in list(running.clips):
                    session.find_clip(c["id"])
                await asyncio.sleep(0.02)
                peak["queue"] = max(peak["queue"], len(running._queue))
                peak["live"] = max(peak["live"], len(running.live))
            await session.stop_session(target)
            return peak

        peak = asyncio.run(go())
        # Bounded: the gate stops reading at MAX_QUEUED pieces or MAX_LIVE
        # turns, and one poll adds at most POLL_EVENTS events.
        assert peak["live"] <= 3 + 4
        assert peak["queue"] <= (3 + 4) * 2
        assert peak["clips"] <= session.MAX_CLIPS
        said = [f["text"] for f in pages.events("say")]
        # The first answer was skipped once heard; every other one, in order.
        assert said == [f"Answer {i}." for i in range(n) if f"Answer {i}." in said]
        assert said[-1] == f"Answer {n - 1}."
        assert len(said) >= n - 1
        assert len(pages.events("turn_end")) == n

    def _done_while_unread(self, tmux, window, monkeypatch, skip_after=None):
        target, win, path = window
        monkeypatch.setattr(session, "MAX_QUEUED", 4)
        monkeypatch.setattr(session, "POLL_EVENTS", 2)
        pages = Pages()
        parts = [f"Part number {i} of the one answer." for i in range(12)]

        async def go():
            running = await _start(target, pages)
            tmux.set(win, "@agent_state", "busy")
            append(
                path,
                prompt("one long answer", "u1"),
                *(
                    say([{"type": "text", "text": t}], f"a{i}")
                    for i, t in enumerate(parts)
                ),
                TURN_END,
            )
            await pages.wait(lambda p: len(p.events("say")) >= session.MAX_AHEAD)
            # The agent finishes while the page is away and lines are unread.
            tmux.set(win, "@agent_state", "done")
            await asyncio.sleep(3 * session.END_GRACE)
            assert pages.events("turn_end") == []
            # The page comes back and fetches as clips arrive.
            deadline = time.monotonic() + 20
            skipped = False
            while not pages.events("turn_end") and time.monotonic() < deadline:
                for c in list(running.clips):
                    session.find_clip(c["id"])
                if (
                    skip_after
                    and not skipped
                    and len(pages.events("say")) >= skip_after
                ):
                    await running.skip()
                    skipped = True
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)
            await session.stop_session(target)

        asyncio.run(go())
        return pages, parts

    def test_done_while_the_end_is_unread_gives_one_turn(
        self, tmux, window, tts_server, fast, monkeypatch
    ):
        pages, parts = self._done_while_unread(tmux, window, monkeypatch)
        assert [f["text"] for f in pages.events("say")] == parts
        assert len(pages.events("turn_end")) == 1
        assert len({f["turn"] for f in pages.events("say")}) == 1
        states = pages.states()
        assert "listening" not in states[states.index("speaking") : -2]
        assert states[-2:] == ["listening", "off"]

    def test_a_truncation_while_paused_does_not_end_the_turn_early(
        self, tmux, window, tts_server, fast, monkeypatch
    ):
        target, win, path = window
        monkeypatch.setattr(session, "MAX_QUEUED", 1)
        monkeypatch.setattr(session, "POLL_EVENTS", 2)
        pages = Pages()

        async def go():
            running = await _start(target, pages)
            tmux.set(win, "@agent_state", "busy")
            append(
                path,
                prompt("q", "u1"),
                *(
                    say([{"type": "text", "text": f"Part {i} of it."}], f"a{i}")
                    for i in range(6)
                ),
            )
            await pages.wait(lambda p: len(p.events("say")) >= session.MAX_AHEAD)
            assert running._backlogged()
            with open(path, "r+") as f:  # rewritten in place, same inode
                f.truncate(0)
                f.write(say([{"type": "text", "text": "Rewritten part."}], "r1"))
            tmux.set(win, "@agent_state", "done")
            await asyncio.sleep(3 * session.END_GRACE)
            ends = len(pages.events("turn_end"))
            await session.stop_session(target)
            return ends

        assert asyncio.run(go()) == 0

    def test_skip_covers_the_whole_actual_turn(
        self, tmux, window, tts_server, fast, monkeypatch
    ):
        pages, parts = self._done_while_unread(tmux, window, monkeypatch, skip_after=4)
        said = [f["text"] for f in pages.events("say")]
        assert said == parts[: len(said)] and len(said) < len(parts)
        assert len(pages.events("turn_end")) == 1

    def test_snapshot_drops_expired_clips_and_the_speaker_moves_on(
        self, tmux, window, tts_server, fast
    ):
        target, win, path = window
        pages = Pages()

        async def go():
            running = await _start(target, pages)
            append(
                path,
                prompt("long", "u1"),
                say([{"type": "text", "text": self.LONG}], "a1"),
            )
            await pages.wait(lambda p: len(p.events("say")) >= session.MAX_AHEAD)
            assert len(running.snapshot()["clips"]) == session.MAX_AHEAD
            session.store.drop([c["id"] for c in running.clips])  # retention expired
            assert running.snapshot()["clips"] == []
            await pages.wait(lambda p: len(p.events("say")) > session.MAX_AHEAD)
            await session.stop_session(target)

        asyncio.run(go())


class TestLifecycle:
    @pytest.fixture
    def fake_tmux(self, monkeypatch, tts_server, fast):
        """Mocked window helpers: a mark that can be watched, and a set-option
        that can be held."""
        state = {"marks": {}, "hold": None}

        def set_opt(target, name, value):
            if state["hold"] is not None:
                state["hold"].wait(5)
            state["marks"][(target, name)] = value
            return True

        def unset_opt(target, name):
            state["marks"].pop((target, name), None)
            return True

        monkeypatch.setattr(sweep, "set_window_option", set_opt)
        monkeypatch.setattr(sweep, "unset_window_option", unset_opt)
        monkeypatch.setattr(
            sweep,
            "window_options",
            lambda t, n: {k: ("claude" if k == "@agent_provider" else "") for k in n},
        )
        monkeypatch.setenv("MERLIN_TTS_FORMAT", "wav")
        return state

    def test_a_stop_during_start_leaves_nothing_behind(self, fake_tmux):
        hold = threading.Event()
        fake_tmux["hold"] = hold
        pages = Pages()

        async def go():
            starting = asyncio.create_task(
                session.start_session("s:@1", "en", broadcast=pages)
            )
            await asyncio.sleep(0.1)  # start is held inside set-option
            stopping = asyncio.create_task(session.stop_session("s:@1", "off"))
            await asyncio.sleep(0.1)
            hold.set()
            running = await starting
            await stopping
            return running

        running = asyncio.run(go())
        assert running.stopped
        assert session.get("s:@1") is None
        assert fake_tmux["marks"] == {}
        assert all(t.done() for t in running._tasks)

    @pytest.mark.parametrize("cancel_after", [0.05, 0.3])
    def test_a_cancelled_start_rolls_back(self, fake_tmux, cancel_after):
        hold = threading.Event()
        fake_tmux["hold"] = hold if cancel_after < 0.1 else None
        pages = Pages()
        created = []
        real_init = VoiceSession.__init__

        def spy(self, *a, **kw):
            real_init(self, *a, **kw)
            created.append(self)

        async def go():
            VoiceSession.__init__ = spy
            try:
                task = asyncio.create_task(
                    session.start_session("s:@2", "en", broadcast=pages)
                )
                await asyncio.sleep(cancel_after if cancel_after < 0.1 else 0)
                task.cancel()
                await asyncio.sleep(0.1)  # the held write lands after the cancel
                hold.set()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                await asyncio.sleep(0.1)
            finally:
                VoiceSession.__init__ = real_init

        asyncio.run(go())
        assert fake_tmux["marks"] == {}
        assert session.get("s:@2") is None
        for s in created:
            assert s.stopped and all(t.done() for t in s._tasks)


class TestBrowserSafeErrors:
    def test_upstream_details_never_reach_the_pages(
        self, tmux, window, fast, monkeypatch
    ):
        from http.server import BaseHTTPRequestHandler

        target, win, path = window

        class Leaky(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps(
                    {
                        "error": {
                            "message": f"bad key sk-SECRET-KEY for {self.server.url}"
                        }
                    }
                ).encode()
                self.send_response(401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Leaky)
        host, port = srv.server_address[:2]
        srv.url = f"http://{host}:{port}/v1"
        threading.Thread(
            target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        monkeypatch.setenv("MERLIN_TTS_URL", srv.url)
        monkeypatch.setenv("MERLIN_TTS_API_KEY", "sk-SECRET-KEY")
        monkeypatch.setenv("MERLIN_TTS_FORMAT", "wav")
        pages = Pages()

        async def go():
            running = await session.start_session(target, "en", broadcast=pages)
            append(
                path,
                prompt("q", "u1"),
                say([{"type": "text", "text": "Hello there."}], "a1"),
            )
            await pages.wait(lambda p: p.events("error"))
            snap = running.snapshot()
            await session.stop_session(target)
            return snap

        try:
            snap = asyncio.run(go())
        finally:
            srv.shutdown()
            srv.server_close()
        everything = json.dumps(pages.frames) + json.dumps(snap)
        assert "sk-SECRET-KEY" not in everything and srv.url not in everything
        assert (
            pages.events("error")[0]["message"]
            == "The voice server refused the key (401)"
        )

    def test_the_log_detail_is_redacted(self):
        from voice import tts

        config = tts.TtsConfig("http://h:1/v1", "k3y", "m", "f", "e", 1.0, "wav")
        assert (
            tts.redact("key k3y at http://h:1/v1", config)
            == "key [redacted] at [redacted]"
        )


class FakeSocket:
    def __init__(self, hold=False):
        self.got = []
        self.closed = None
        self.release = asyncio.Event() if hold else None

    async def send_text(self, frame):
        if self.release is not None:
            await self.release.wait()
        self.got.append(json.loads(frame[1:]))

    async def close(self, code=1000):
        self.closed = code


class TestPageIsolation:
    def test_a_stalled_page_is_closed_and_does_not_hold_the_others(self, monkeypatch):
        from terminal import routes as terminal_routes

        monkeypatch.setattr(terminal_routes, "CONTROL_SEND_TIMEOUT", 0.3)

        async def go():
            stalled_ws, healthy_ws = FakeSocket(hold=True), FakeSocket()
            stalled, healthy = (
                terminal_routes.SessionReportState(),
                terminal_routes.SessionReportState(),
            )
            stalled.websocket, healthy.websocket = stalled_ws, healthy_ws
            monkeypatch.setattr(terminal_routes, "_client_views", {stalled, healthy})
            started = time.monotonic()
            for i in range(3):
                await terminal_routes.broadcast_control({"n": i})
            returned = time.monotonic() - started
            await asyncio.sleep(0.05)
            got_quickly = [f["n"] for f in healthy_ws.got]
            await asyncio.sleep(0.5)
            # The stalled page was closed (it reconnects and resyncs) and
            # gets no more frames; the healthy one keeps getting them.
            await terminal_routes.broadcast_control({"n": 3})
            await asyncio.sleep(0.05)
            return returned, got_quickly, stalled, stalled_ws, healthy_ws

        returned, got, stalled, stalled_ws, healthy_ws = asyncio.run(go())
        assert returned < 0.05
        assert got == [0, 1, 2]
        assert stalled_ws.closed == terminal_routes.CONTROL_CLOSE_CODE
        assert stalled.websocket is None and len(stalled.outbox) == 0
        assert [f["n"] for f in healthy_ws.got] == [0, 1, 2, 3]

    def test_overflow_during_an_in_flight_send(self, monkeypatch):
        from terminal import routes as terminal_routes

        monkeypatch.setattr(terminal_routes, "CONTROL_OUTBOX_LIMIT", 2)
        errors = []

        async def go():
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda l, c: errors.append(c))
            ws = FakeSocket(hold=True)
            state = terminal_routes.SessionReportState()
            state.websocket = ws
            monkeypatch.setattr(terminal_routes, "_client_views", {state})
            await terminal_routes.broadcast_control({"n": 0})
            await asyncio.sleep(0.01)  # frame 0 is in flight, held
            await terminal_routes.broadcast_control({"n": 1})
            await terminal_routes.broadcast_control({"n": 2})
            await terminal_routes.broadcast_control({"n": 3})  # overflow
            await asyncio.sleep(0.01)
            ws.release.set()
            await asyncio.sleep(0.05)
            sender = state.sender
            return ws, state, sender

        ws, state, sender = asyncio.run(go())
        assert ws.closed == terminal_routes.CONTROL_CLOSE_CODE
        assert state.websocket is None and len(state.outbox) == 0
        assert sender.done() and not sender.cancelled() and sender.exception() is None
        assert errors == []
        assert [f["n"] for f in ws.got] == [0]  # the in-flight frame, nothing else

    def test_injection_does_not_wait_on_pages(self, monkeypatch):
        from terminal import routes as terminal_routes

        state = terminal_routes.SessionReportState()
        state.websocket = FakeSocket(hold=True)
        monkeypatch.setattr(terminal_routes, "_client_views", {state})

        class Running:
            async def note_dictation(self, text, error):
                await session.VoiceSession._send(self, "state", state="thinking")

            target = "s:@1"

            async def broadcast(self, payload):
                await terminal_routes.broadcast_control(payload)

        monkeypatch.setattr(session, "_sessions", {"s:@1": Running()})

        async def go():
            started = time.monotonic()
            await terminal_routes._note_voice("s:@1", "hello", None)
            return time.monotonic() - started

        assert asyncio.run(go()) < 0.05
