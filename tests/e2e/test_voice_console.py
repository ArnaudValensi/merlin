"""E2E: the voice console, end to end, against a throwaway Merlin.

Chromium's fake mic plays a real spoken sentence (``fixtures/voice-utterance.wav``,
synthesized once with Kokoro, then silence, looped), so the vendored Silero VAD
runs for real. Transcription is the deterministic stub (``MERLIN_TRANSCRIBE_FAKE``),
speech goes to a fake OpenAI-compatible TTS server in this process, and the agent
is ``fixtures/fake_voice_agent.py`` in a tmux window: it stamps the options the
agent hooks would, and writes a Claude Code transcript for each prompt.

What runs for real: the Voice button and its window check, the console, the
VAD, the utterance upload through dictation's path (202, the background task,
send-keys into the window), the voice session following the transcript, the
control frames over the terminal socket, the audio fetched by id and played
through WebAudio, Yes/No on the ask state, Exit clearing ``@voice``.

Run: uv run scripts.py test-e2e
Requires: chromium + tmux.
"""

import io
import json
import shutil
import socket
import subprocess
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="web terminal needs tmux"
)

FIXTURES = Path(__file__).parent / "fixtures"
UTTERANCE = FIXTURES / "voice-utterance.wav"
AGENT = FIXTURES / "fake_voice_agent.py"
MARKER = "VOICEPROMPT77"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TTS_PORT = _free_port()

MERLIN_OPTIONS = {
    # auto: the server installs the agent hooks into its throwaway HOME, which
    # voice mode requires before it starts.
    "config": "AGENT_STATE_HOOKS=auto\n",
    "extra_env": {
        "OPENAI_API_KEY": "sk-e2e-test",
        "MERLIN_TRANSCRIBE_FAKE": MARKER,
        "MERLIN_TRANSCRIBE_DELAY": "0.3",
        "MERLIN_TTS_URL": f"http://127.0.0.1:{TTS_PORT}/v1",
    },
}


# ---------------------------------------------------------------------------
# A fake OpenAI-compatible TTS server
# ---------------------------------------------------------------------------


def _beep(seconds=0.35, rate=24000) -> bytes:
    import math

    frames = b"".join(
        int(8000 * math.sin(2 * math.pi * 440 * i / rate)).to_bytes(
            2, "little", signed=True
        )
        for i in range(int(seconds * rate))
    )
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return out.getvalue()


class _Tts(BaseHTTPRequestHandler):
    inputs: list[str] = []
    audio = _beep()
    # The ask sentence lasts long enough to be caught playing.
    ask_audio = _beep(seconds=4.0)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).inputs.append(body["input"])
        audio = self.ask_audio if "needs your answer" in body["input"] else self.audio
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def tts():
    server = ThreadingHTTPServer(("127.0.0.1", TTS_PORT), _Tts)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield _Tts
    server.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmux(env, *args):
    return subprocess.run(["tmux", *args], env=env, capture_output=True, text=True)


def _capture(env, target):
    return _tmux(env, "capture-pane", "-p", "-J", "-S", "-200", "-t", target).stdout


def _option(env, target, name):
    window = target.split(":", 1)[1]
    return _tmux(env, "show-options", "-w", "-v", "-t", window, name).stdout.strip()


def _wait(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(message)


def _agent_window(env, session, tmp_path, mode="answer"):
    transcript = tmp_path / "transcript.jsonl"
    control = tmp_path / "control"
    control.write_text(mode)
    window = _tmux(
        env,
        "new-window",
        "-d",
        "-t",
        session,
        "-P",
        "-F",
        "#{window_id}",
        f"python3 {AGENT} {transcript} {control}",
    ).stdout.strip()
    target = f"{session}:{window}"
    _wait(
        lambda: "FAKE AGENT READY" in _capture(env, target), 10, "agent never started"
    )
    return target, control


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(
            headless=True,
            args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                f"--use-file-for-fake-audio-capture={UTTERANCE}",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        yield b
        b.close()


def _open(browser, server, viewport=None):
    ctx = browser.new_context(
        viewport=viewport or {"width": 1100, "height": 760}, permissions=["microphone"]
    )
    page = ctx.new_page()
    page.goto(f"{server}/terminal")
    page.wait_for_selector(".xterm-screen", timeout=30000)
    page.wait_for_function(
        "() => window.MerlinTerminal && window.MerlinTerminal.currentWindow()",
        timeout=20000,
    )
    # Record every voice frame and every clip fetched, in order.
    page.evaluate(
        """() => {
            window.__frames = [];
            const v = window.MerlinVoice;
            const orig = v.onFrame;
            v.onFrame = (f) => { window.__frames.push(f); orig(f); };
        }"""
    )
    fetched: list[str] = []
    page.on(
        "request",
        lambda r: (
            fetched.append(r.url.rsplit("/", 1)[1])
            if "/api/voice/audio/" in r.url
            else None
        ),
    )
    return ctx, page, fetched


def _switch(page, target):
    window = target.split(":", 1)[1]
    page.evaluate("t => window.MerlinTerminal.switchSession(t)", target)
    page.wait_for_function(
        f"() => window.MerlinTerminal.currentWindow() === '{window}'", timeout=10000
    )


def _mode(page):
    return page.evaluate("() => window.MerlinVoice.mode()")


def _wait_mode(page, modes, timeout=30000):
    page.wait_for_function(
        f"() => {json.dumps(list(modes))}.includes(window.MerlinVoice.mode())",
        timeout=timeout,
    )


def _start_voice(page, target):
    _switch(page, target)
    page.wait_for_function(
        "() => !document.getElementById('voice-btn').classList.contains('voice-mode-unavailable')",
        timeout=10000,
    )
    page.click("#voice-btn")
    page.wait_for_selector("#voice-console:not([hidden])", timeout=5000)
    _wait_mode(page, ["listen", "tx"], timeout=60000)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def test_a_spoken_turn_goes_round_and_the_console_listens_again(
    browser, server, tmux_env, tts, tmp_path
):
    ctx, page, fetched = _open(browser, server)
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        target, _ = _agent_window(tmux_env, session, tmp_path)
        _start_voice(page, target)
        assert _option(tmux_env, target, "@voice") == "on"

        # The VAD hears the sentence, the utterance is uploaded with this
        # window as target, and the transcription lands at its prompt.
        _wait(
            lambda: MARKER in _capture(tmux_env, target), 30, "the prompt never landed"
        )

        # The answer is spoken: captions, activity, clips in say order.
        page.wait_for_function(
            "() => window.__frames.some(f => f.event === 'turn_end')", timeout=30000
        )
        _wait_mode(page, ["listen", "tx", "proc"], timeout=30000)
        frames = page.evaluate("() => window.__frames")
        events = [f["event"] for f in frames]
        assert all(f["target"] == target for f in frames)
        heard = [f["text"] for f in frames if f["event"] == "heard"]
        assert heard and heard[0] == MARKER
        assert any(f["event"] == "activity" and f["label"] == "command" for f in frames)
        says = [f for f in frames if f["event"] == "say"]
        assert " ".join(s["text"] for s in says).startswith(
            "Running the tests now. All 42 tests pass. Nothing else to do."
        )
        assert events.index("heard") < events.index("say") < events.index("turn_end")
        # Every clip fetched, in the order announced, each once.
        said_ids = [s["id"] for s in says]
        assert fetched[: len(said_ids)] == said_ids
        assert "Running the tests now." in tts.inputs
        assert not any("*" in text for text in tts.inputs)

        captions = page.inner_text("#voice-console .vc-captions")
        assert MARKER in captions and "Nothing else to do." in captions

        # A reload finds the session still running: the console holds until a
        # tap unlocks audio and the mic again, then carries on.
        page.reload()
        page.wait_for_selector("#voice-console:not([hidden])", timeout=20000)
        assert _mode(page) == "resume"
        assert _option(tmux_env, target, "@voice") == "on"
        page.click("#voice-console .vc-scope")
        _wait_mode(page, ["listen", "tx", "proc", "rx"], timeout=30000)

        # Exit clears the mark and hides the console.
        page.click("#voice-console [data-act=exit]")
        page.wait_for_selector("#voice-console", state="hidden", timeout=5000)
        _wait(
            lambda: _option(tmux_env, target, "@voice") == "",
            5,
            "@voice was not cleared",
        )
    finally:
        ctx.close()


def test_ask_shows_yes_and_no_and_they_send_enter_and_escape(
    browser, server, tmux_env, tts, tmp_path
):
    ctx, page, _ = _open(browser, server, viewport={"width": 390, "height": 844})
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        target, _ = _agent_window(tmux_env, session, tmp_path, mode="ask")
        # Hold the first ask sentence's download (the only long clip) until
        # after Yes: it must never start once the user has answered.
        held = []

        def hold_ask_audio(route):
            response = route.fetch()
            if len(response.body()) > 100_000 and not held:
                held.append((route, response))
                return
            route.fulfill(response=response)

        page.route("**/api/voice/audio/*", hold_ask_audio)
        _start_voice(page, target)

        _wait_mode(page, ["ask"], timeout=40000)
        assert "Allow Bash: pytest?" in page.inner_text("#voice-console .vc-excerpt")
        assert page.is_visible("#voice-console [data-act=yes]")
        assert page.is_visible("#voice-console [data-act=no]")
        assert not page.is_visible("#voice-console [data-act=stop]")
        _wait(lambda: held, 10, "the ask sentence was never requested")
        # Hold the answer too, so the dialog is still up when the download
        # lands: only the revoked token keeps the sentence from starting.
        answers = []
        page.route("**/api/voice/answer", lambda r: answers.append(r))
        page.click("#voice-console [data-act=yes]")
        _wait(lambda: answers, 5, "Yes was never sent")
        route, response = held[0]
        route.fulfill(response=response)
        page.unroute("**/api/voice/audio/*")
        page.wait_for_timeout(1000)
        assert _mode(page) == "ask"
        assert page.evaluate("() => window.MerlinVoice.speaking()") is None
        answers[0].continue_()
        page.unroute("**/api/voice/answer")
        _wait(
            lambda: "ANSWER:YES" in _capture(tmux_env, target),
            10,
            "Yes did not send Enter",
        )

        # The fake mic loops, so the next turn comes by itself: answer No.
        _wait_mode(page, ["listen", "tx", "proc", "rx"], timeout=20000)
        _wait_mode(page, ["ask"], timeout=60000)
        # This time the sentence is playing, in the one speech slot: No cuts it.
        page.wait_for_function(
            "() => window.MerlinVoice.speaking() === 'ask'", timeout=10000
        )
        page.click("#voice-console [data-act=no]")
        assert page.evaluate("() => window.MerlinVoice.speaking()") is None
        _wait(
            lambda: "ANSWER:NO" in _capture(tmux_env, target),
            10,
            "No did not send Escape",
        )

        # Exit while the ask sentence plays stops it.
        _wait_mode(page, ["ask"], timeout=60000)
        page.wait_for_function(
            "() => window.MerlinVoice.speaking() === 'ask'", timeout=10000
        )
        page.click("#voice-console [data-act=exit]")
        assert page.evaluate("() => window.MerlinVoice.speaking()") is None
        _wait(
            lambda: _option(tmux_env, target, "@voice") == "",
            5,
            "@voice was not cleared",
        )

    finally:
        ctx.close()


def test_a_resync_holds_listening_until_a_snapshot_lands(
    browser, server, tmux_env, tts, tmp_path
):
    """A reconnect asks for the session's state; while that request fails
    the page neither listens nor plays, then carries on once it succeeds."""
    ctx, page, _ = _open(browser, server)
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        target, _ = _agent_window(tmux_env, session, tmp_path)
        _start_voice(page, target)
        calls = []

        def flaky_session(route):
            calls.append(route.request.url)
            if len(calls) <= 2:
                route.abort()
            else:
                route.continue_()

        page.route("**/api/voice/session?*", flaky_session)
        state = page.evaluate(
            """() => {
                document.dispatchEvent(new CustomEvent('merlin:terminal-socket'));
                return [window.MerlinVoice.mode(), window.MerlinVoice.listening(),
                        window.MerlinVoice.model().syncing];
            }"""
        )
        assert state == ["proc", False, True], state
        page.wait_for_timeout(1500)
        assert page.evaluate("() => window.MerlinVoice.listening()") is False
        page.wait_for_function(
            "() => !window.MerlinVoice.model().syncing", timeout=15000
        )
        assert len(calls) >= 3
        _wait_mode(page, ["listen", "tx", "proc", "rx"], timeout=10000)
        page.click("#voice-console [data-act=exit]")
    finally:
        ctx.close()


def test_exit_while_starting_then_open_another_window(
    browser, server, tmux_env, tts, tmp_path
):
    """Exit lands while the start request is held: the late answer must not
    take over, and the next open (on another window) runs its own start."""
    ctx, page, _ = _open(browser, server)
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        first, _ = _agent_window(tmux_env, session, tmp_path / "a")
        second, _ = _agent_window(tmux_env, session, tmp_path / "b")
        held = []

        def hold_first_start(route):
            if (
                json.loads(route.request.post_data or "{}").get("target") == first
                and not held
            ):
                held.append(route)
                return
            route.continue_()

        page.route("**/api/voice/start", hold_first_start)
        _switch(page, first)
        page.wait_for_function(
            "() => !document.getElementById('voice-btn').classList.contains('voice-mode-unavailable')",
            timeout=10000,
        )
        page.click("#voice-btn")
        _wait(lambda: held, 10, "the first start never went out")
        page.click("#voice-console [data-act=exit]")
        page.wait_for_selector("#voice-console", state="hidden", timeout=5000)

        _start_voice(page, second)
        assert page.evaluate("() => window.MerlinVoice.target()") == second
        held[0].continue_()
        page.wait_for_timeout(1500)
        assert page.evaluate("() => window.MerlinVoice.target()") == second
        _wait_mode(page, ["listen", "tx", "proc", "rx"], timeout=10000)
        page.click("#voice-console [data-act=exit]")
        _wait(
            lambda: _option(tmux_env, second, "@voice") == "", 5, "second kept @voice"
        )
        # The held start reached the server after Exit: the page stops it.
        _wait(lambda: _option(tmux_env, first, "@voice") == "", 10, "first kept @voice")
    finally:
        ctx.close()


def test_a_window_without_claude_says_why_and_a_closed_window_ends_the_console(
    browser, server, tmux_env, tts, tmp_path
):
    ctx, page, _ = _open(browser, server)
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        shell = _tmux(
            tmux_env, "new-window", "-d", "-t", session, "-P", "-F", "#{window_id}"
        ).stdout.strip()
        _switch(page, f"{session}:{shell}")
        page.wait_for_function(
            "() => document.getElementById('voice-btn').classList.contains('voice-mode-unavailable')",
            timeout=10000,
        )
        assert "No Claude Code session" in page.get_attribute("#voice-btn", "title")
        page.click("#voice-btn")
        page.wait_for_function(
            "() => document.querySelector('.xterm-rows').innerText.includes('No Claude Code session')",
            timeout=5000,
        )
        assert page.is_hidden("#voice-console")

        target, _ = _agent_window(tmux_env, session, tmp_path)
        _start_voice(page, target)
        _tmux(tmux_env, "kill-window", "-t", target)
        page.wait_for_selector("#voice-console", state="hidden", timeout=10000)
        page.wait_for_function(
            "() => document.querySelector('.xterm-rows').innerText.includes('The window was closed')",
            timeout=5000,
        )
    finally:
        ctx.close()
