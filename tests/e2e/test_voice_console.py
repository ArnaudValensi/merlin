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
        # Each piece ends with the end word: one piece, one message.
        "MERLIN_TRANSCRIBE_FAKE": f"{MARKER}, terminé",
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


FAKE_MEDIA = [
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
]


@pytest.fixture(scope="module")
def playwright():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright):
    """The fake mic plays the spoken sentence, looped."""
    b = playwright.chromium.launch(
        headless=True,
        args=[*FAKE_MEDIA, f"--use-file-for-fake-audio-capture={UTTERANCE}"],
    )
    yield b
    b.close()


@pytest.fixture(scope="module")
def quiet_browser(playwright):
    """Chromium's default fake mic (a beep, never speech): the VAD stays
    quiet, so the clips a test announces are all that happens."""
    b = playwright.chromium.launch(headless=True, args=FAKE_MEDIA)
    yield b
    b.close()


def _open(browser, server, viewport=None, scale=1, view="full"):
    ctx = browser.new_context(
        viewport=viewport or {"width": 1100, "height": 760},
        device_scale_factor=scale,
        permissions=["microphone"],
    )
    page = ctx.new_page()
    # Most scenarios tap the full-screen console; a wide screen opens the bar.
    if view:
        page.add_init_script(f"localStorage.setItem('voice-view', '{view}')")
    # Track every mic stream: one per session, since a reopen asks Safari's
    # permission again. Stub Safari's navigator.audioSession too: the answer
    # must play under "play-and-record" (the open mic), which the iPhone
    # silent switch does not mute, and exit must go back to "auto".
    page.add_init_script(
        """(() => {
            window.__mics = [];
            window.__answerSeen = false;
            window.__sessionAtMic = [];
            window.__sessionDuringAnswer = new Set();
            if (!navigator.audioSession) {
                Object.defineProperty(navigator, "audioSession", { value: { type: "auto" } });
            }
            const md = navigator.mediaDevices;
            const get = md.getUserMedia.bind(md);
            md.getUserMedia = async (c) => {
                window.__sessionAtMic.push(navigator.audioSession.type);
                const s = await get(c);
                window.__mics.push(s);
                return s;
            };
            setInterval(() => {
                const v = window.MerlinVoice;
                if (!v || v.speaking() !== "answer") return;
                window.__answerSeen = true;
                window.__sessionDuringAnswer.add(navigator.audioSession.type);
            }, 20);
        })();"""
    )
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

        # The VAD hears the sentence, its piece is transcribed, ends with the
        # end word, and the message lands at this window's prompt without it.
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

        # No conversation text on screen: neither what was said nor the answer.
        shown = page.inner_text("#voice-console")
        assert MARKER not in shown and "Nothing else to do." not in shown
        assert "command" not in shown.lower()

        # One mic for the whole session, open while the answer played, under
        # play-and-record (the silent switch does not mute it).
        assert page.evaluate("() => window.__answerSeen")
        assert page.evaluate("() => window.__mics.length") == 1
        assert page.evaluate("() => [...window.__sessionDuringAnswer]") == [
            "play-and-record"
        ]
        assert page.evaluate("() => window.__sessionAtMic") == ["play-and-record"]
        assert "terminé" not in _capture(tmux_env, target)

        # A reload finds the session still running: the console holds until a
        # tap unlocks audio and the mic again, then carries on.
        page.reload()
        page.wait_for_selector("#voice-console:not([hidden])", timeout=20000)
        assert _mode(page) == "resume"
        assert _option(tmux_env, target, "@voice") == "on"
        page.click("#voice-console .vc-mode")
        _wait_mode(page, ["listen", "tx", "proc", "rx"], timeout=30000)

        # Exit clears the mark, hides the console, closes the mic and gives
        # the audio session back (dictation's mic would be refused otherwise).
        page.click("#voice-console [data-act=exit]")
        page.wait_for_selector("#voice-console", state="hidden", timeout=5000)
        _wait(
            lambda: _option(tmux_env, target, "@voice") == "",
            5,
            "@voice was not cleared",
        )
        assert page.evaluate("() => navigator.audioSession.type") == "auto"
        assert not page.evaluate(
            "() => window.__mics.some(s => s.getAudioTracks().some(t => t.readyState === 'live'))"
        )
    finally:
        ctx.close()


def test_a_message_waits_for_its_end_word(browser, server, tmux_env, tts, tmp_path):
    """Pauses do not send: the pieces said are held until one ends with the
    end word, then go as one message, without it."""
    ctx, page, _ = _open(browser, server)
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        target, _ = _agent_window(tmux_env, session, tmp_path)
        # Two pieces without the end word, then (once the test checked that
        # nothing was sent) one with it. Pieces in between hear nothing.
        texts = ["Lance les tests", "puis le lint."]
        served = []
        release = []

        def transcribe(route):
            if texts:
                text = texts.pop(0)
            else:
                text = "Terminé." if release else ""
            served.append(text)
            route.fulfill(json={"text": text})

        def pump_until(predicate, message):
            # The route handler runs only while Playwright has control: wait
            # through the page, not with sleep.
            for _ in range(150):
                if predicate():
                    return
                page.wait_for_timeout(200)
            raise AssertionError(message)

        page.route("**/api/terminal/transcribe", transcribe)
        _start_voice(page, target)
        pump_until(lambda: len(served) >= 3, "the pieces never came")
        assert "Lance les tests" not in _capture(tmux_env, target)
        assert _mode(page) == "tx" and page.evaluate(
            "() => window.MerlinVoice.model().held"
        )
        release.append(True)
        pump_until(
            lambda: "Lance les tests puis le lint." in _capture(tmux_env, target),
            "the message never landed",
        )
        assert "Terminé" not in _capture(tmux_env, target)
        page.click("#voice-console [data-act=exit]")
        page.wait_for_selector("#voice-console", state="hidden", timeout=5000)
        _tmux(tmux_env, "kill-window", "-t", target)
    finally:
        ctx.close()


@pytest.mark.parametrize(
    "viewport,scale",
    [({"width": 1600, "height": 980}, 2), ({"width": 390, "height": 844}, 3)],
    ids=["desktop", "phone"],
)
def test_the_console_fits_the_screen_at_a_high_pixel_ratio(
    browser, server, tmux_env, tts, tmp_path, viewport, scale
):
    # The scope's canvas buffer follows its displayed size times the pixel
    # ratio: in the flow, it grew the scope every frame and pushed EXIT off.
    ctx, page, _ = _open(browser, server, viewport=viewport, scale=scale)
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        target, _ = _agent_window(tmux_env, session, tmp_path)
        _start_voice(page, target)
        box = "() => document.querySelector('#voice-console .vc-scope').getBoundingClientRect().height"
        first = page.evaluate(box)
        page.wait_for_timeout(800)
        assert page.evaluate(box) == first
        exit_box = page.locator("#voice-console .vc-exit").bounding_box()
        plate = page.locator("#voice-console .vc-plate").bounding_box()
        assert exit_box and plate
        assert exit_box["y"] + exit_box["height"] <= plate["y"] + plate["height"] + 0.5
        assert plate["y"] + plate["height"] <= viewport["height"] + 0.5
        # End the session and drop the window: the server and the tmux session
        # outlive this page, and a later test lands on a neighbouring window.
        page.click("#voice-console [data-act=exit]")
        page.wait_for_selector("#voice-console", state="hidden", timeout=5000)
        _wait(
            lambda: _option(tmux_env, target, "@voice") == "",
            5,
            "@voice was not cleared",
        )
        _tmux(tmux_env, "kill-window", "-t", target)
    finally:
        ctx.close()


def test_a_wide_screen_opens_the_bar_and_the_terminal_stays_in_use(
    browser, server, tmux_env, tts, tmp_path
):
    ctx, page, _ = _open(browser, server, view=None)
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        target, _ = _agent_window(tmux_env, session, tmp_path)
        height = "() => document.getElementById('terminal-container').getBoundingClientRect().height"
        before = page.evaluate(height)
        _start_voice(page, target)

        # The bar sits in the terminal's column: xterm keeps the rest of it.
        assert page.get_attribute("#voice-console", "data-view") == "dock"
        assert _option(tmux_env, target, "@voice") == "screen"
        bar = page.locator("#voice-console").bounding_box()
        status = page.locator("#terminal-status").bounding_box()
        assert bar and status and bar["height"] < 80
        assert bar["y"] + bar["height"] <= status["y"] + 0.5
        page.wait_for_function(f"() => ({height})() < {before}", timeout=5000)
        assert page.evaluate(height) > before - 100
        assert page.is_visible(".xterm-screen")
        # No two ears on the one mic.
        assert page.is_hidden("#mic-btn")
        assert not page.is_visible("#voice-console [data-act=tap]")

        # A click on the bar is not a tap: the buttons do that here.
        page.evaluate(
            """() => window.MerlinVoice.onFrame({
                type: "voice", event: "state", state: "thinking",
                target: window.MerlinVoice.target(),
                session: window.MerlinVoice.model().session,
                rev: window.MerlinVoice.model().rev + 1,
            })"""
        )
        _wait_mode(page, ["proc"], timeout=5000)
        page.click("#voice-console .vc-mode")
        assert _mode(page) == "proc"
        assert page.is_visible("#voice-console [data-act=stop]")

        # Full screen covers the terminal, and the agent writes for the ear.
        page.click("#voice-console [data-view-toggle]")
        assert page.get_attribute("#voice-console", "data-view") == "full"
        _wait(lambda: _option(tmux_env, target, "@voice") == "on", 5, "@voice not on")
        box = page.locator("#voice-console").bounding_box()
        assert box and box["height"] >= 700
        # The choice stays for the next session.
        page.click("#voice-console [data-view-toggle]")
        _wait(lambda: _option(tmux_env, target, "@voice") == "screen", 5, "not screen")
        assert page.evaluate("() => localStorage.getItem('voice-view')") == "dock"

        page.click("#voice-console [data-act=exit]")
        page.wait_for_selector("#voice-console", state="hidden", timeout=5000)
        assert page.is_visible("#mic-btn")
        _wait(
            lambda: _option(tmux_env, target, "@voice") == "", 5, "@voice not cleared"
        )
        _tmux(tmux_env, "kill-window", "-t", target)
    finally:
        ctx.close()


@pytest.mark.parametrize(
    "width,height",
    [(390, 844), (768, 1024), (1280, 800), (1600, 900)],
    ids=["phone", "tablet", "desk", "wide"],
)
def test_the_bar_keeps_its_height_through_a_turn_and_grows_only_for_an_error(
    quiet_browser, server, tmux_env, tts, tmp_path, width, height
):
    ctx, page, _ = _open(
        quiet_browser,
        server,
        viewport={"width": width, "height": height},
        view="dock",
    )
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        target, _ = _agent_window(tmux_env, session, tmp_path)
        _start_voice(page, target)

        # What a turn goes through, the longest question included, forced
        # straight onto the model: the layout is what is under test.
        force = """([state, text]) => {
            const v = window.MerlinVoice, m = v.model();
            m.local = state === "tx" ? "hearing" : "idle";
            m.ask = state === "ask" ? {excerpt: "", text, audio: null} : null;
            m.playing = state === "rx" ? {id: "x", status: "playing", answer: "x"} : null;
            m.queue = []; m.held = false;
            m.error = state === "error" ? text : "";
            const server = {proc: "thinking", ask: "ask", error: "error"}[state] || "listening";
            v.onFrame({type: "voice", event: "state", state: server, target: m.target,
                       session: m.session, rev: m.rev + 1});
            return v.mode();
        }"""
        bar = "() => document.getElementById('voice-console').getBoundingClientRect().height"
        terminal = "() => document.getElementById('terminal-container').getBoundingClientRect().height"
        question = "Allow the agent to run the deployment script? " * 4
        heights, rows = {}, {}
        for state, text in (
            ("listen", ""),
            ("tx", ""),
            ("proc", ""),
            ("rx", ""),
            ("ask", question),
            ("listen", ""),
        ):
            assert page.evaluate(force, [state, text]) == state
            heights[state] = page.evaluate(bar)
            rows[state] = page.evaluate(terminal)
        assert len(set(heights.values())) == 1, heights
        # One row, on a phone too.
        assert heights["listen"] < 50, heights
        assert len(set(rows.values())) == 1, rows
        assert page.evaluate(
            "() => { const p = document.querySelector('#voice-console .vc-plate');"
            " return p.scrollWidth <= p.clientWidth; }"
        )

        # A question that fits has nothing more to offer.
        assert page.evaluate(force, ["ask", "Allow it?"]) == "ask"
        assert not page.is_visible("#voice-console .vc-ask-more")
        page.evaluate(force, ["listen", ""])

        # An answer that failed says so, and the question stays with its buttons.
        assert page.evaluate(force, ["ask", question]) == "ask"
        page.evaluate(
            """() => {
                const v = window.MerlinVoice, m = v.model();
                m.notice = "The answer did not go through";
                v.onFrame({type: "voice", event: "state", state: "ask", target: m.target,
                           session: m.session, rev: m.rev + 1});
            }"""
        )
        assert page.is_visible("#voice-console .vc-notice")
        assert page.is_visible("#voice-console .vc-ask-text")
        assert page.is_visible("#voice-console [data-act=yes]")
        page.evaluate(
            """() => {
                const n = document.querySelector("#voice-console .vc-notice");
                n.classList.remove("on"); n.textContent = "";
            }"""
        )
        page.evaluate(force, ["listen", ""])

        # A question too long for its line offers the rest, on the user's tap.
        assert page.evaluate(force, ["ask", question]) == "ask"
        # The window it is for stays named once the bar has the width for it.
        bar_width = page.evaluate(
            "() => document.getElementById('voice-console').clientWidth"
        )
        assert page.is_visible("#voice-console .vc-window") == (bar_width >= 640)
        more = page.locator("#voice-console .vc-ask-more")
        assert more.is_visible() and more.get_attribute("aria-expanded") == "false"
        more.click()
        assert more.get_attribute("aria-expanded") == "true"
        assert page.evaluate(bar) > heights["listen"]
        assert page.evaluate(
            "() => { const t = document.querySelector('#voice-console .vc-ask-text');"
            " return t.scrollWidth <= t.clientWidth + 1 && t.getBoundingClientRect().height > 30; }"
        )
        page.evaluate(force, ["listen", ""])
        assert page.evaluate(bar) == heights["listen"]

        # An error is the exception: it is read whole, so the bar makes room.
        message = "The voice server could not reach the speech service, " * 3
        assert page.evaluate(force, ["error", message]) == "error"
        assert page.evaluate(bar) > heights["listen"]
        assert page.evaluate(
            "() => { const e = document.querySelector('#voice-console .vc-error');"
            " return e.scrollHeight <= e.clientHeight + 1 && e.scrollWidth <= e.clientWidth; }"
        )

        # No sign of life in a failure.
        assert not page.is_visible("#voice-console .vc-scope")
        # The server's error has nothing to retry: no button, no promise.
        assert not page.is_visible("#voice-console [data-act=tap]")
        assert page.inner_text("#voice-console .vc-hint") == "Exit to start again"

        page.evaluate(force, ["listen", ""])
        page.click("#voice-console [data-act=exit]")
        page.wait_for_selector("#voice-console", state="hidden", timeout=5000)
        _tmux(tmux_env, "kill-window", "-t", target)
    finally:
        ctx.close()


def test_full_screen_is_a_dialog_with_a_real_button_for_the_tap(
    quiet_browser, server, tmux_env, tts, tmp_path
):
    ctx, page, _ = _open(quiet_browser, server, view="dock")
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        target, _ = _agent_window(tmux_env, session, tmp_path)
        _start_voice(page, target)
        terminal_inert = "() => document.getElementById('terminal-container').closest('[inert]') !== null"
        assert not page.evaluate(terminal_inert)

        # From the keyboard, a button of the bar keeps the keys on the bar; the
        # mouse hands them back to the terminal.
        thinking = """() => {
            const v = window.MerlinVoice, m = v.model();
            v.onFrame({type: "voice", event: "state", state: "thinking", target: m.target,
                       session: m.session, rev: m.rev + 1});
        }"""
        page.evaluate(thinking)
        page.focus("#voice-console [data-act=stop]")
        page.keyboard.press("Enter")
        page.wait_for_timeout(50)
        assert page.evaluate(
            "() => document.getElementById('voice-console').contains(document.activeElement)"
        )
        page.evaluate(thinking)
        page.click("#voice-console [data-act=stop]")
        page.wait_for_function(
            "() => document.activeElement.classList.contains('xterm-helper-textarea')",
            timeout=3000,
        )
        # The language is the status bar's picker, and the session hears of it.
        assert not page.is_visible("#voice-console .vc-lang")
        with page.expect_request("**/api/voice/lang") as told:
            page.select_option("#mic-lang", "fr")
        assert json.loads(told.value.post_data)["lang"] == "fr"
        page.select_option("#mic-lang", "en")
        page.evaluate(
            """() => {
                const v = window.MerlinVoice, m = v.model();
                v.onFrame({type: "voice", event: "state", state: "listening", target: m.target,
                           session: m.session, rev: m.rev + 1});
            }"""
        )

        page.click("#voice-console [data-view-toggle]")
        page.wait_for_timeout(50)
        assert page.evaluate(terminal_inert)
        assert page.evaluate("() => document.activeElement.id") == "voice-console"
        # Everything it covers, the sidebar outside the terminal's own column too.
        assert (
            page.evaluate(
                """() => [...document.querySelectorAll("a[href], button, textarea")]
                .filter((n) => !n.closest("#voice-console") && !n.closest("[inert]")).length"""
            )
            == 0
        )
        page.evaluate(
            """() => {
                const v = window.MerlinVoice, m = v.model();
                m.playing = {id: "x", status: "playing", answer: "x"};
                v.onFrame({type: "voice", event: "state", state: "listening", target: m.target,
                           session: m.session, rev: m.rev + 1});
            }"""
        )
        assert _mode(page) == "rx"
        tap = page.locator("#voice-console [data-act=tap]")
        assert tap.is_visible() and tap.inner_text() == "Skip"

        page.evaluate(
            """() => {
                const v = window.MerlinVoice, m = v.model();
                m.playing = null; m.queue = [];
                v.onFrame({type: "voice", event: "state", state: "error", target: m.target,
                           session: m.session, rev: m.rev + 1});
            }"""
        )
        assert _mode(page) == "error"
        assert "retry" not in page.inner_text("#voice-console .vc-hint").lower()

        page.evaluate(
            """() => {
                const v = window.MerlinVoice, m = v.model();
                v.onFrame({type: "voice", event: "state", state: "listening", target: m.target,
                           session: m.session, rev: m.rev + 1});
            }"""
        )
        page.click("#voice-console [data-view-toggle]")
        assert not page.evaluate(terminal_inert)
        page.wait_for_function(
            "() => document.activeElement && document.activeElement.classList.contains('xterm-helper-textarea')",
            timeout=3000,
        )
        page.click("#voice-console [data-act=exit]")
        page.wait_for_selector("#voice-console", state="hidden", timeout=5000)
        _tmux(tmux_env, "kill-window", "-t", target)
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
        shown = page.inner_text("#voice-console")
        assert "The agent needs your answer." in shown
        assert "Allow Bash: pytest?" not in shown  # no pane excerpt on screen
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


SAY = """([target, clips]) => {
    const m = window.MerlinVoice.model();
    for (const [id, turn] of clips) {
        window.MerlinVoice.onFrame({type: 'voice', event: 'say', target, session: m.session,
                                    rev: m.rev + 1, id, turn, text: 'clip ' + id});
    }
}"""


def _serve_clips(page, plan):
    """Answer /api/voice/audio/<id> per ``plan[id]``: 'long' (a 4 s clip),
    'short', or 'fail' (aborted). Returns the ids requested, in order."""
    long_clip, short_clip = _beep(4.0), _beep(0.3)
    asked = []

    def handle(route):
        clip = route.request.url.rsplit("/", 1)[1]
        asked.append(clip)
        how = plan.get(clip, "fail")
        if how == "fail":
            route.abort()
        else:
            body = long_clip if how == "long" else short_clip
            route.fulfill(status=200, body=body, headers={"Content-Type": "audio/wav"})

    page.route("**/api/voice/audio/*", handle)
    return asked


def test_skip_answer_stops_the_clip_of_that_answer_still_playing(
    quiet_browser, server, tmux_env, tts, tmp_path
):
    ctx, page, _ = _open(quiet_browser, server)
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        target, _ = _agent_window(tmux_env, session, tmp_path)
        _start_voice(page, target)
        # Clip A of answer 101 plays for 4 s while its clip B fails three times.
        _serve_clips(page, {"clipA": "long"})
        page.evaluate(SAY, [target, [["clipA", 101], ["clipB", 101]]])
        _wait_mode(page, ["error"], timeout=10000)
        assert page.evaluate("() => window.MerlinVoice.speaking()") == "answer"
        assert "could not be downloaded" in page.inner_text("#voice-console .vc-error")
        assert page.is_visible("#voice-console [data-act=skip]")
        page.click("#voice-console [data-act=skip]")
        assert page.evaluate("() => window.MerlinVoice.speaking()") is None
        assert page.evaluate("() => window.MerlinVoice.model().queue.length") == 0
        _wait_mode(page, ["listen", "tx"], timeout=5000)
        page.click("#voice-console [data-act=exit]")
    finally:
        ctx.close()


def test_skipping_one_stalled_answer_keeps_the_other_recoverable(
    quiet_browser, server, tmux_env, tts, tmp_path
):
    ctx, page, _ = _open(quiet_browser, server)
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        target, _ = _agent_window(tmux_env, session, tmp_path)
        _start_voice(page, target)
        plan = {}
        asked = _serve_clips(page, plan)
        page.evaluate(SAY, [target, [["clipX", 201], ["clipY", 202]]])
        page.wait_for_function(
            "() => window.MerlinVoice.model().queue.every(c => c.status === 'stalled')",
            timeout=10000,
        )
        _wait_mode(page, ["error"], timeout=5000)
        page.click("#voice-console [data-act=skip]")
        # Y is still stuck: the error, its retry and its skip stay.
        assert _mode(page) == "error"
        assert page.is_visible("#voice-console [data-act=skip]")
        queue = page.evaluate(
            "() => window.MerlinVoice.model().queue.map(c => [c.id, c.status])"
        )
        assert queue == [["clipY", "stalled"]]
        # The network is back for Y: a tap retries it and it is heard once.
        plan["clipY"] = "short"
        page.click("#voice-console .vc-mode")
        page.wait_for_function(
            "() => window.MerlinVoice.model().played.includes('clipY')", timeout=10000
        )
        played = page.evaluate("() => window.MerlinVoice.model().played")
        assert played.count("clipY") == 1 and "clipX" not in played
        assert asked.count("clipY") == 4  # three failures, then the retry
        _wait_mode(page, ["listen", "tx"], timeout=5000)
        page.click("#voice-console [data-act=exit]")
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
        # Record the notices rather than read the screen: tmux redraws it on
        # the window it lands on, and that redraw can wipe the line.
        page.evaluate(
            """() => {
                const T = window.MerlinTerminal, show = T.notice;
                window.__notices = [];
                T.notice = (msg) => { window.__notices.push(msg); show(msg); };
            }"""
        )
        _tmux(tmux_env, "kill-window", "-t", target)
        page.wait_for_selector("#voice-console", state="hidden", timeout=10000)
        page.wait_for_function(
            "() => window.__notices.some(n => n.includes('The window was closed'))",
            timeout=5000,
        )
    finally:
        ctx.close()
