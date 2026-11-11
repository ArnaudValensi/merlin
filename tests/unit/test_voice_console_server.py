"""The server side of the voice console: the window check behind the Voice
button, the console's assets (the vendored VAD cached for good, the rest
never), and the button on the terminal page."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main as app_mod
from board import sweep
from lib import skills
from voice import routes, session

VENDOR = routes.STATIC_DIR / "vendor"


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


class TestCheck:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setattr(session, "_sessions", {})
        app = FastAPI()
        app.include_router(routes.api_router, prefix="/api/voice")
        return TestClient(app)

    @pytest.fixture
    def window(self, monkeypatch):
        """A window whose options the test sets, and every write recorded:
        the check must never touch the window."""
        options = {
            "@agent_state": "idle",
            "@agent_provider": "claude",
            "@agent_transcript": "",
            "@agent_transcript_mark": "",
            "@agent_conv": "",
        }
        writes = []
        monkeypatch.setenv("MERLIN_TTS_URL", "http://127.0.0.1:9/v1")
        monkeypatch.setattr(sweep, "window_options", lambda t, n: dict(options))
        monkeypatch.setattr(
            sweep, "set_window_option", lambda *a: writes.append(a) or True
        )
        monkeypatch.setattr(
            sweep, "unset_window_option", lambda *a: writes.append(a) or True
        )
        monkeypatch.setattr(skills, "voice_hooks_installed", lambda: True)
        return options, writes

    def check(self, client, target="s:@1"):
        r = client.get("/api/voice/check", params={"target": target})
        assert r.status_code == 200
        return r.json()

    def test_a_view_without_a_session_is_refused_and_writes_nothing(
        self, client, window
    ):
        _, writes = window
        r = client.post("/api/voice/view", json={"target": "s:@1", "view": "dock"})
        assert r.status_code == 404
        assert writes == []

    def test_a_claude_window_can_start(self, client, window):
        _, writes = window
        assert self.check(client) == {
            "available": True,
            "reason": None,
            "target": "s:@1",
        }
        assert writes == []

    @pytest.mark.parametrize(
        "provider,reason",
        [
            ("codex", "Claude Code only"),
            ("", "No Claude Code session"),
        ],
    )
    def test_other_windows_say_why(self, client, window, provider, reason):
        options, writes = window
        options["@agent_provider"] = provider
        got = self.check(client)
        assert got["available"] is False and reason in got["reason"]
        assert writes == []

    def test_no_voice_server(self, client, window, monkeypatch):
        monkeypatch.delenv("MERLIN_TTS_URL")
        got = self.check(client)
        assert got["available"] is False and "No voice server" in got["reason"]

    def test_hooks_missing(self, client, window, monkeypatch):
        monkeypatch.setattr(skills, "voice_hooks_installed", lambda: False)
        got = self.check(client)
        assert got["available"] is False and "hooks" in got["reason"]

    def test_a_window_that_is_gone(self, client, window, monkeypatch):
        monkeypatch.setattr(sweep, "window_options", lambda t, n: None)
        assert self.check(client)["reason"] == "The window is gone"

    def test_not_a_window(self, client, window):
        assert self.check(client, "nonsense")["reason"] == "Not a terminal window"


class TestAssets:
    @pytest.fixture
    def client(self):
        with TestClient(app_mod.app) as c:
            yield c

    def test_the_vendored_vad_is_cached_for_good(self, client):
        for path in (
            "vad-web-0.0.30/vad.worklet.bundle.min.js",
            "onnxruntime-web-1.22.0/ort.wasm.min.js",
        ):
            r = client.get(f"/static/voice/vendor/{path}")
            assert r.status_code == 200
            assert r.headers["cache-control"] == "public, max-age=31536000, immutable"

    def test_the_console_itself_is_never_cached(self, client):
        for path in ("voice-console.js", "voice-core.js", "voice-console.css"):
            r = client.get(f"/static/voice/{path}")
            assert r.status_code == 200
            assert r.headers["cache-control"] == "no-store"

    def test_a_missing_vendor_file_is_not_cached(self, client):
        r = client.get("/static/voice/vendor/vad-web-0.0.30/nope.js")
        assert r.status_code == 404
        assert r.headers["cache-control"] == "no-store"

    def test_every_vendored_file_sits_in_a_versioned_directory(self):
        """The immutable cache is only safe while a new version is a new path."""
        for path in VENDOR.rglob("*"):
            if path.is_file() and path.name != "README.md":
                folder = path.relative_to(VENDOR).parts[0]
                assert folder.rsplit("-", 1)[1][0].isdigit(), folder

    def test_the_vendored_files_match_the_readme(self):
        import hashlib

        readme = (VENDOR / "README.md").read_text()
        for path in VENDOR.rglob("*"):
            if path.is_file() and path.name != "README.md":
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                assert f"{digest}  {path.relative_to(VENDOR)}" in readme, path


class TestTerminalPage:
    def test_the_voice_button_carries_the_capability(self, monkeypatch):
        monkeypatch.setenv("MERLIN_TTS_URL", "http://127.0.0.1:9/v1")
        with TestClient(app_mod.app) as c:
            html = c.get("/terminal").text
        assert 'id="voice-btn"' in html
        assert 'id="voice-console"' in html
        assert "&#34;available&#34;: true" in html
        assert "/static/voice/voice-console.js" in html

    def test_without_a_voice_server_the_button_knows_why(self, monkeypatch):
        monkeypatch.delenv("MERLIN_TTS_URL", raising=False)
        with TestClient(app_mod.app) as c:
            html = c.get("/terminal").text
        assert "&#34;available&#34;: false" in html
        assert "No voice server configured" in html


class TestSay:
    """POST /api/voice/say types a message into the window at most once: a
    retry with the same id after a lost answer is answered without typing."""

    TARGET = "s:@1"

    @pytest.fixture
    def keys(self, monkeypatch):
        """The keys sent to the window, and switches to make them fail."""
        sent: list[tuple] = []
        fail = {"literal": False, "enter": False}

        def literal(target, text):
            if fail["literal"]:
                return False
            sent.append(("text", text))
            return True

        def enter(target):
            if fail["enter"]:
                return False
            sent.append(("enter",))
            return True

        monkeypatch.setattr(sweep, "exit_copy_mode", lambda t: None)
        monkeypatch.setattr(sweep, "send_literal", literal)
        monkeypatch.setattr(sweep, "send_enter", enter)
        return sent, fail

    @pytest.fixture
    def running(self, monkeypatch):
        config = routes.tts.TtsConfig(
            url="http://127.0.0.1:9/v1",
            api_key="",
            model="kokoro",
            voice_fr="ff_siwis",
            voice_en="af_heart",
            speed=1.0,
            format="wav",
        )
        voice = session.VoiceSession(
            self.TARGET, "en", config=config, provider="claude"
        )
        noted: list[str | None] = []

        async def note(text, error):
            noted.append(text)

        monkeypatch.setattr(voice, "note_dictation", note)
        monkeypatch.setattr(session, "_sessions", {self.TARGET: voice})
        voice.noted = noted
        return voice

    @pytest.fixture
    def client(self, running):
        app = FastAPI()
        app.include_router(routes.api_router, prefix="/api/voice")
        return TestClient(app)

    def say(self, client, text="Run the tests", **extra):
        return client.post(
            "/api/voice/say", json={"target": self.TARGET, "text": text, **extra}
        )

    def test_a_repeated_id_is_answered_without_typing_again(
        self, client, keys, running
    ):
        sent, _ = keys
        assert self.say(client, id="m1").json() == {"ok": True}
        assert self.say(client, id="m1").json() == {"ok": True}
        assert sent == [("text", "Run the tests"), ("enter",)]
        assert running.noted == ["Run the tests"]
        # Another message is typed.
        assert self.say(client, "Then the lint", id="m2").json() == {"ok": True}
        assert sent[-2:] == [("text", "Then the lint"), ("enter",)]

    def test_without_an_id_every_request_types_as_before(self, client, keys):
        sent, _ = keys
        assert self.say(client).json() == {"ok": True}
        assert self.say(client).json() == {"ok": True}
        assert sent.count(("text", "Run the tests")) == 2

    def test_a_message_the_window_refused_is_typed_by_its_retry(self, client, keys):
        sent, fail = keys
        fail["literal"] = True
        assert self.say(client, id="m1").status_code == 502
        fail["literal"] = False
        assert self.say(client, id="m1").json() == {"ok": True}
        assert sent == [("text", "Run the tests"), ("enter",)]

    def test_a_retry_after_the_text_went_but_not_its_enter_sends_only_enter(
        self, client, keys
    ):
        sent, fail = keys
        fail["enter"] = True
        assert self.say(client, id="m1").status_code == 502
        fail["enter"] = False
        assert self.say(client, id="m1").json() == {"ok": True}
        assert sent == [("text", "Run the tests"), ("enter",)]

    def test_a_retry_while_the_first_still_types_waits_and_does_not_type(
        self, keys, running, monkeypatch
    ):
        sent, _ = keys
        started = None
        release = None

        async def go():
            nonlocal started, release
            started, release = asyncio.Event(), asyncio.Event()
            loop = asyncio.get_running_loop()

            async def slow_thread(fn, *args):
                # The first literal blocks until the retry is waiting on it.
                if fn is sweep.send_literal and not started.is_set():
                    started.set()
                    await release.wait()
                return fn(*args)

            monkeypatch.setattr(routes.asyncio, "to_thread", slow_thread)
            body = routes.SayBody(target=self.TARGET, text="Run the tests", id="m1")
            first = loop.create_task(routes.api_say(body))
            await started.wait()
            second = loop.create_task(routes.api_say(body))
            await asyncio.sleep(0.05)
            # The first holds the message while the second waits on it.
            assert running.message("m1").lock.locked()
            assert not second.done()
            release.set()
            return await first, await second

        first, second = asyncio.run(go())
        assert first == {"ok": True} and second == {"ok": True}
        assert sent == [("text", "Run the tests"), ("enter",)]
        assert running.noted == ["Run the tests"]

    def test_the_session_remembers_its_last_ids_only(self, running):
        first = running.message("m0")
        for i in range(1, session.MAX_MESSAGES + 1):
            running.message(f"m{i}")
        assert running.message("m0") is not first
        assert running.message(f"m{session.MAX_MESSAGES}") is running.message(
            f"m{session.MAX_MESSAGES}"
        )

    def test_an_overlong_id_is_refused(self, client, keys):
        sent, _ = keys
        assert self.say(client, id="x" * 65).status_code == 422
        assert sent == []
