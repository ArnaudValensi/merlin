"""E2E tests for inline audio audition in the file browser.

The File Browser listing plays an audio sample in place when its leading icon is
tapped, without navigating to the file detail view. These tests drive a real
browser against a live server to prove: the listing stays visible, exactly one
sample plays at a time, toggling stops it, switching moves the marker, and
navigating away stops playback.

Run with: uv run scripts.py test-e2e   (or)
          uv run --with pytest --with playwright pytest tests/e2e/test_audio_audition.py -v
Requires: uv run --with playwright playwright install firefox
"""

import os
import signal
import socket
import struct
import subprocess
import time
import wave

import pytest

# Skip all tests if playwright is not installed
pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _write_wav(path, seconds=2.0, freq=440.0, rate=8000):
    """Write a short, real, decodable mono WAV so the browser can play it."""
    n = int(seconds * rate)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        # A quiet sine so playback is real without being loud in a dev run.
        import math

        for i in range(n):
            sample = int(3000 * math.sin(2 * math.pi * freq * (i / rate)))
            frames += struct.pack("<h", sample)
        w.writeframes(bytes(frames))


@pytest.fixture(scope="module")
def sample_files(tmp_path_factory):
    """A folder of small audio samples plus a non-audio file and a subdir."""
    root = tmp_path_factory.mktemp("audiotest")
    samples = root / "samples"
    samples.mkdir()
    _write_wav(samples / "one.wav", freq=440.0)
    _write_wav(samples / "two.wav", freq=550.0)
    _write_wav(samples / "three.wav", freq=660.0)
    (samples / "notes.txt").write_text("not audio\n")
    return samples


@pytest.fixture(scope="module")
def server(sample_files):
    """Start the Merlin server without auth on a random port."""
    port = _find_free_port()
    env = os.environ.copy()
    env["DASHBOARD_PASS"] = ""
    env["MERLIN_SAAS_TOKEN"] = ""
    env["DISCORD_BOT_TOKEN"] = ""
    env["DISCORD_CHANNEL_IDS"] = ""

    merlin_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    env.pop("MERLIN_HOME", None)

    proc = subprocess.Popen(
        ["uv", "run", "main.py", "--port", str(port)],
        cwd=merlin_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    url = f"http://localhost:{port}"
    for _ in range(30):
        try:
            import urllib.request

            urllib.request.urlopen(f"{url}/api/files/browse?path=/tmp", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("Server failed to start")

    yield url

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def browser_context(server):
    """A Playwright context with autoplay allowed so click-to-play really plays."""
    with sync_playwright() as p:
        browser = p.firefox.launch(
            headless=True,
            firefox_user_prefs={
                "media.autoplay.default": 0,
                "media.autoplay.blocking_policy": 0,
            },
        )
        ctx = browser.new_context(viewport={"width": 1200, "height": 800})
        yield ctx, server
        ctx.close()
        browser.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_listing(ctx, url, sample_files):
    page = ctx.new_page()
    page.goto(f"{url}/files{sample_files}", wait_until="networkidle")
    # Generous timeout: the very first page load of the module warms Firefox and
    # the vendor JS bundle, which can exceed a few seconds on a cold start.
    page.wait_for_selector(".dir-entry-play", timeout=15000)
    return page


def _play_button(page, name):
    return page.query_selector(f'.dir-entry-play[data-audition-path$="/{name}"]')


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInlineAudition:
    def test_audio_rows_get_a_play_control(self, browser_context, sample_files):
        ctx, url = browser_context
        page = _open_listing(ctx, url, sample_files)

        # Every .wav row has a play button; the .txt row does not.
        assert _play_button(page, "one.wav") is not None
        assert _play_button(page, "two.wav") is not None
        assert _play_button(page, "notes.txt") is None

        btn = _play_button(page, "one.wav")
        assert btn.get_attribute("aria-label") == "Play one.wav"
        assert btn.get_attribute("aria-pressed") == "false"

        page.close()

    def test_click_plays_inline_without_navigating(self, browser_context, sample_files):
        ctx, url = browser_context
        page = _open_listing(ctx, url, sample_files)
        before = page.url

        _play_button(page, "one.wav").click()
        page.wait_for_selector(".dir-entry.playing", timeout=5000)

        # The listing is still showing; the detail view never opened.
        assert page.query_selector("#dir-view").is_visible()
        file_view = page.query_selector("#file-view")
        assert file_view is None or not file_view.is_visible()
        assert page.url == before, "Auditioning must not change the URL"

        # Exactly the clicked row is marked playing, with a stop affordance.
        playing = page.query_selector_all(".dir-entry.playing")
        assert len(playing) == 1
        btn = _play_button(page, "one.wav")
        assert btn.get_attribute("aria-label") == "Stop one.wav"
        assert btn.get_attribute("aria-pressed") == "true"

        page.close()

    def test_click_again_stops(self, browser_context, sample_files):
        ctx, url = browser_context
        page = _open_listing(ctx, url, sample_files)

        _play_button(page, "one.wav").click()
        page.wait_for_selector(".dir-entry.playing", timeout=5000)
        _play_button(page, "one.wav").click()
        page.wait_for_selector(".dir-entry.playing", state="detached", timeout=5000)

        assert len(page.query_selector_all(".dir-entry.playing")) == 0
        assert _play_button(page, "one.wav").get_attribute("aria-pressed") == "false"

        page.close()

    def test_switching_moves_the_marker(self, browser_context, sample_files):
        ctx, url = browser_context
        page = _open_listing(ctx, url, sample_files)

        _play_button(page, "one.wav").click()
        page.wait_for_selector(".dir-entry.playing", timeout=5000)
        _play_button(page, "two.wav").click()

        # Wait until two.wav is the one marked playing.
        page.wait_for_function(
            """() => {
                const p = document.querySelectorAll('.dir-entry.playing');
                if (p.length !== 1) return false;
                const btn = p[0].querySelector('.dir-entry-play');
                return btn && btn.dataset.auditionPath.endsWith('/two.wav');
            }""",
            timeout=5000,
        )

        assert len(page.query_selector_all(".dir-entry.playing")) == 1
        assert _play_button(page, "one.wav").get_attribute("aria-pressed") == "false"
        assert _play_button(page, "two.wav").get_attribute("aria-pressed") == "true"

        page.close()

    def test_opening_a_file_stops_playback(self, browser_context, sample_files):
        ctx, url = browser_context
        page = _open_listing(ctx, url, sample_files)

        _play_button(page, "one.wav").click()
        page.wait_for_selector(".dir-entry.playing", timeout=5000)

        # Tapping the row body (not the play control) opens the detail view.
        # That navigation must stop the inline audition.
        page.click(
            '.dir-entry:has(.dir-entry-play[data-audition-path$="/three.wav"]) '
            ".dir-entry-name"
        )
        page.wait_for_selector("#file-view", state="visible", timeout=5000)
        page.wait_for_function(
            "() => document.querySelectorAll('.dir-entry.playing').length === 0",
            timeout=5000,
        )

        assert len(page.query_selector_all(".dir-entry.playing")) == 0

        page.close()
