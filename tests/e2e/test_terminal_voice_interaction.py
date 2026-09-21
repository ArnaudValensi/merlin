"""E2E: the transcription lands in the target window through the real path.

These drive a real headless Chromium (fake mic) against a throwaway Merlin whose
transcription backend is a deterministic stub (MERLIN_TRANSCRIBE_FAKE returns a
known marker after a short delay), so the recording, the 202, the background
task and the tmux injection all run for real over the private tmux server, and
the marker is checked with `tmux capture-pane` on the window the recording named.
The stub replaces only the Whisper call, not the paths under test.

The four scenarios mirror `requirements.md` "Manual, on the throwaway instance":
the text lands in the window the recording stopped in even after the page
switches window (A) or a second page is elsewhere (B), it still lands at the
prompt when the target pane is scrolled into copy-mode (C), and a window closed
between stop and injection loses the text with a logged warning and nothing
written elsewhere (D).

The marker text is fixed for the whole server, so every scenario records into a
window it creates fresh, and asserts against fresh windows, so one scenario's
marker can never be mistaken for another's. Two tmux clients on one session share
the current window, so the cross-page case (B) and the vanished case (D) use a
second session, which is how per-device window views actually work.

Run: uv run scripts.py test-e2e
Requires: chromium + tmux.
"""

import shutil
import subprocess
import time

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="web terminal needs tmux"
)

MARKER = "VOICEMARK42"

# OPENAI_API_KEY makes the mic button available; MERLIN_TRANSCRIBE_FAKE short
# circuits the backend to return MARKER after MERLIN_TRANSCRIBE_DELAY, giving the
# test a window to switch or kill before the delayed injection fires.
MERLIN_OPTIONS = {
    "extra_env": {
        "OPENAI_API_KEY": "sk-e2e-test",
        "MERLIN_TRANSCRIBE_FAKE": MARKER,
        "MERLIN_TRANSCRIBE_DELAY": "1.0",
    }
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmux(env, *args):
    return subprocess.run(["tmux", *args], env=env, capture_output=True, text=True)


def _new_window(env, session):
    out = _tmux(
        env, "new-window", "-d", "-t", session, "-P", "-F", "#{window_id}"
    ).stdout.strip()
    assert out.startswith("@"), f"new-window gave {out!r}"
    return out


def _capture(env, target):
    return _tmux(env, "capture-pane", "-p", "-t", target).stdout


def _open_terminal(browser, server):
    ctx = browser.new_context(
        viewport={"width": 1100, "height": 720}, permissions=["microphone"]
    )
    page = ctx.new_page()
    page.goto(f"{server}/terminal")
    page.wait_for_selector(".xterm-screen", timeout=30000)
    page.wait_for_function(
        "() => window.MerlinTerminal && window.MerlinTerminal.currentWindow()",
        timeout=20000,
    )
    return ctx, page


def _switch(page, target, want_window):
    page.evaluate("t => window.MerlinTerminal.switchSession(t)", target)
    page.wait_for_function(
        f"() => window.MerlinTerminal.currentWindow() === '{want_window}'",
        timeout=10000,
    )


def _record_and_stop(page, ms=500):
    page.click("#mic-btn")
    page.wait_for_timeout(ms)
    page.click("#mic-btn")


def _wait_for_marker(env, target, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if MARKER in _capture(env, target):
            return True
        time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(
            headless=True,
            args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
            ],
        )
        yield b
        b.close()


# ---------------------------------------------------------------------------
# A. Switch window after stop: the text stays where it was dictated
# ---------------------------------------------------------------------------


def test_text_lands_in_the_window_the_recording_stopped_in(browser, server, tmux_env):
    ctx, page = _open_terminal(browser, server)
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        win_a = _new_window(tmux_env, session)
        win_b = _new_window(tmux_env, session)
        _switch(page, f"{session}:{win_a}", win_a)

        _record_and_stop(page)
        # Move to B before the delayed injection fires. The text still belongs
        # to A, the window that was current at the stop tap.
        _switch(page, f"{session}:{win_b}", win_b)

        assert _wait_for_marker(tmux_env, f"{session}:{win_a}"), (
            "marker never reached A"
        )
        assert MARKER not in _capture(tmux_env, f"{session}:{win_b}")
        # The page did not get dragged back to A.
        assert page.evaluate("() => window.MerlinTerminal.currentWindow()") == win_b
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# B. Two pages, two sessions: each recording lands in its own window
# ---------------------------------------------------------------------------


def test_a_second_page_elsewhere_never_receives_the_text(browser, server, tmux_env):
    ctx1, page1 = _open_terminal(browser, server)
    ctx2, page2 = _open_terminal(browser, server)
    try:
        session1 = page1.evaluate("() => window.MerlinTerminal.currentSession()")
        # A separate session is the only way two clients view different windows.
        # Move the second page there first, so selecting page1's window later
        # cannot drag it along.
        assert _tmux(tmux_env, "new-session", "-d", "-s", "sess_b").returncode == 0
        win2 = _tmux(
            tmux_env, "display-message", "-p", "-t", "sess_b", "#{window_id}"
        ).stdout.strip()
        _switch(page2, "sess_b", win2)

        win1 = _new_window(tmux_env, session1)
        _switch(page1, f"{session1}:{win1}", win1)

        _record_and_stop(page1)
        assert _wait_for_marker(tmux_env, f"{session1}:{win1}"), (
            "marker never reached page1's window"
        )
        assert MARKER not in _capture(tmux_env, f"sess_b:{win2}")
    finally:
        ctx1.close()
        ctx2.close()


# ---------------------------------------------------------------------------
# C. A scrolled (copy-mode) target still receives the text at the prompt
# ---------------------------------------------------------------------------


def test_copy_mode_target_still_lands_at_the_prompt(browser, server, tmux_env):
    ctx, page = _open_terminal(browser, server)
    try:
        session = page.evaluate("() => window.MerlinTerminal.currentSession()")
        win = _new_window(tmux_env, session)
        _switch(page, f"{session}:{win}", win)
        target = f"{session}:{win}"

        assert _tmux(tmux_env, "copy-mode", "-t", target).returncode == 0
        time.sleep(0.2)
        in_mode = _tmux(
            tmux_env, "display-message", "-p", "-t", target, "#{pane_in_mode}"
        ).stdout.strip()
        assert in_mode == "1"

        _record_and_stop(page)
        assert _wait_for_marker(tmux_env, target), "marker never reached the pane"
        after = _tmux(
            tmux_env, "display-message", "-p", "-t", target, "#{pane_in_mode}"
        ).stdout.strip()
        assert after == "0", "copy-mode was not cancelled"
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# D. A window closed before injection loses the text, with a logged warning
# ---------------------------------------------------------------------------


def test_vanished_window_logs_a_warning_and_writes_nothing(
    browser, server, tmux_env, merlin
):
    ctx, page = _open_terminal(browser, server)
    try:
        # A dedicated session so the fallback window is provably clean (no other
        # scenario ever wrote here).
        assert _tmux(tmux_env, "new-session", "-d", "-s", "sess_d").returncode == 0
        keep = _tmux(
            tmux_env, "display-message", "-p", "-t", "sess_d", "#{window_id}"
        ).stdout.strip()
        doomed = _new_window(tmux_env, "sess_d")
        _switch(page, f"sess_d:{doomed}", doomed)

        _record_and_stop(page)
        # Kill the target during the injection delay, before the text is sent.
        assert _tmux(tmux_env, "kill-window", "-t", f"sess_d:{doomed}").returncode == 0
        # Give the background task time to try and fail.
        time.sleep(2.0)

        # The page fell back to sess_d's remaining window, which never held the
        # marker: nothing was written anywhere in the target's session.
        assert MARKER not in _capture(tmux_env, f"sess_d:{keep}")

        log = (merlin.home / "logs" / "merlin.log").read_text(errors="replace")
        assert "Transcription injection failed" in log
        assert doomed in log
    finally:
        ctx.close()
