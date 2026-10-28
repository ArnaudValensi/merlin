"""The server side of the voice console: the window check behind the Voice
button, the console's assets (the vendored VAD cached for good, the rest
never), and the button on the terminal page."""

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
