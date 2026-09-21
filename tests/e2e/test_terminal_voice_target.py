"""E2E: a voice recording carries the window it stopped in as its target.

The cross-window / cross-device bug was that the server wrote a transcription
into whatever window was current when the upload landed. The fix: the page
reads the current tmux window the instant the recording stops and sends it as
the multipart `target` field, so the server injects into that window and
nothing else. tests/unit/test_terminal.py checks the server honours `target`.
This file checks the page actually sends it, by driving a real recording in a
headless Chromium (fake mic) and asserting the intercepted upload body carries
`target=<session>:<window_id>` matching the window the page was showing.

Run: uv run scripts.py test-e2e   (or pytest tests/e2e/test_terminal_voice_target.py)
Requires: chromium + tmux.
"""

import shutil

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="web terminal needs tmux"
)

# Voice needs a transcription backend for the mic button to record at all. A
# dummy OpenAI key satisfies the availability check, and the upload is
# intercepted before it reaches the server, so the key is never actually used.
MERLIN_OPTIONS = {"extra_env": {"OPENAI_API_KEY": "sk-e2e-test"}}


def test_recording_sends_the_stopped_window_as_target(server):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
            ],
        )
        try:
            ctx = browser.new_context(
                viewport={"width": 1100, "height": 720},
                permissions=["microphone"],
            )
            page = ctx.new_page()

            # Swallow the upload with a 202 so no real transcription runs.
            page.route(
                "**/api/terminal/transcribe",
                lambda route: route.fulfill(
                    status=202,
                    content_type="application/json",
                    body='{"status": "accepted"}',
                ),
            )

            page.goto(f"{server}/terminal")
            page.wait_for_selector(".xterm-screen", timeout=30000)
            # The page learns its window from the first session control frame.
            page.wait_for_function(
                "() => window.MerlinTerminal && window.MerlinTerminal.currentWindow()",
                timeout=20000,
            )
            target = page.evaluate(
                "() => window.MerlinTerminal.currentSession() + ':'"
                " + window.MerlinTerminal.currentWindow()"
            )
            assert ":@" in target, f"unexpected target shape: {target!r}"

            # Record, then stop. onstop must capture `target` synchronously,
            # before the first await, and carry it into the upload.
            with page.expect_request(
                "**/api/terminal/transcribe", timeout=20000
            ) as req_info:
                page.click("#mic-btn")
                page.wait_for_timeout(700)  # let the recorder gather audio
                page.click("#mic-btn")

            body = req_info.value.post_data_buffer
            assert body is not None
            assert b'name="target"' in body, "the form carried no target field"
            assert target.encode("utf-8") in body, (
                f"target {target!r} not found in the upload body"
            )
            ctx.close()
        finally:
            browser.close()
