"""WebRTC streaming of an app to a real browser, on this machine's network.

The browser picks the codec (H.264 when it can decode it, NVENC on an NVIDIA
machine); a page that only advertises VP8 exercises the software fallback.
"""

import os
import shutil
import signal
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from app_stream_support import (  # noqa: E402
    APP_OPTIONS,
    close_to,
    launch_probe,
    pixel,
    requires_streaming,
    stop_all,
    streamer_pids,
    wait_for_lines,
    wait_live,
)

MERLIN_OPTIONS = APP_OPTIONS
pytestmark = requires_streaming

GREEN = (0, 255, 136)
MAGENTA = (255, 0, 255)


@pytest.fixture(scope="module")
def probe(merlin, tmp_path_factory):
    log = tmp_path_factory.mktemp("probe") / "probe.log"
    handle = launch_probe(merlin, log)
    yield handle, log
    stop_all(merlin)


@pytest.fixture(scope="module")
def playwright():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright):
    browser = playwright.chromium.launch()
    yield browser
    browser.close()


VP8_ONLY = """
const real = RTCRtpReceiver.getCapabilities.bind(RTCRtpReceiver);
RTCRtpReceiver.getCapabilities = (kind) => {
    const caps = real(kind);
    if (kind === 'video' && caps) {
        caps.codecs = caps.codecs.filter((c) => !/H264/i.test(c.mimeType));
    }
    return caps;
};
"""


def _player(browser, server, viewport=(1280, 720), vp8_only=False):
    context = browser.new_context(
        viewport={"width": viewport[0], "height": viewport[1]}
    )
    if vp8_only:
        context.add_init_script(VP8_ONLY)
    page = context.new_page()
    page.goto(f"{server}/apps/probe/play")
    return context, page


def test_stream_shows_the_app_and_takes_input(browser, server, probe):
    handle, log = probe
    context, page = _player(browser, server)
    try:
        wait_live(page)
        assert page.get_attribute("#player", "data-codec") in ("H264", "VP8")
        assert page.evaluate(
            "[document.getElementById('player-video').videoWidth,"
            " document.getElementById('player-video').videoHeight]"
        ) == [1280, 720]
        # Let a few frames arrive, then sample the probe's colors.
        time.sleep(1.0)
        assert close_to(pixel(page, 900, 600), GREEN), pixel(page, 900, 600)
        assert close_to(pixel(page, 20, 20), MAGENTA), pixel(page, 20, 20)

        page.mouse.click(300, 200)
        page.keyboard.press("x")
        wait_for_lines(
            log, ["btndown 1 300 200", "btnup 1 300 200", "keydown x", "keyup x"]
        )
    finally:
        context.close()


def test_new_viewer_replaces_the_old_and_cleanup(browser, server, probe):
    handle, _ = probe
    first_ctx, first = _player(browser, server)
    wait_live(first)
    second_ctx, second = _player(browser, server)
    try:
        wait_live(second)
        first.wait_for_function(
            "document.getElementById('player').dataset.streamState === 'replaced'",
            timeout=10000,
        )
        assert "another device" in first.inner_text("#player-status")
        assert len(streamer_pids(handle["display"])) == 1
    finally:
        first_ctx.close()
        second_ctx.close()
    deadline = time.monotonic() + 10
    while streamer_pids(handle["display"]) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert streamer_pids(handle["display"]) == []


def test_viewer_disconnect_captures_a_thumbnail(merlin, browser, server, probe):
    thumb = Path(merlin.home) / "data" / "apps" / "thumbs" / "probe.png"
    thumb.unlink(missing_ok=True)  # earlier tests' viewers already left one
    context, page = _player(browser, server)
    wait_live(page)
    context.close()
    deadline = time.monotonic() + 10
    while not thumb.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert thumb.stat().st_size > 0


def test_unknown_app_reports_exited(browser, server, probe):
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(f"{server}/apps/ghost/play")
        page.wait_for_function(
            "document.getElementById('player').dataset.streamState === 'exited'",
            timeout=10000,
        )
    finally:
        context.close()


def test_h264_prefers_nvenc(browser, server, probe):
    context, page = _player(browser, server)
    try:
        wait_live(page)
        assert page.get_attribute("#player", "data-codec") == "H264"
        encoder = page.get_attribute("#player", "data-encoder")
        if shutil.which("nvidia-smi"):
            assert encoder == "nvh264enc"
        else:
            assert encoder in ("nvh264enc", "openh264enc")
    finally:
        context.close()


def test_vp8_fallback(browser, server, probe):
    context, page = _player(browser, server, vp8_only=True)
    try:
        wait_live(page)
        assert page.get_attribute("#player", "data-codec") == "VP8"
        assert page.get_attribute("#player", "data-encoder") == "vp8enc"
        time.sleep(1.0)
        assert close_to(pixel(page, 900, 600), GREEN), pixel(page, 900, 600)
    finally:
        context.close()


@pytest.mark.skipif(shutil.which("chromium") is None, reason="no system chromium")
def test_system_chromium(playwright, server, probe):
    browser = playwright.chromium.launch(executable_path=shutil.which("chromium"))
    try:
        context, page = _player(browser, server)
        wait_live(page)
        assert page.get_attribute("#player", "data-codec") == "H264"
        time.sleep(1.0)
        assert close_to(pixel(page, 900, 600), GREEN), pixel(page, 900, 600)
        context.close()
    finally:
        browser.close()


def test_concurrent_viewers_end_with_exactly_one(browser, server, probe):
    handle, _ = probe
    contexts = []
    try:
        first_ctx, first = _player(browser, server)
        contexts.append(first_ctx)
        wait_live(first)
        pages = [first]
        for _ in range(2):  # two more arrive together
            ctx = browser.new_context(viewport={"width": 1280, "height": 720})
            contexts.append(ctx)
            page = ctx.new_page()
            page.goto(f"{server}/apps/probe/play", wait_until="commit")
            pages.append(page)
        deadline = time.monotonic() + 30
        states = []
        while time.monotonic() < deadline:
            states = [p.get_attribute("#player", "data-stream-state") for p in pages]
            if states.count("live") == 1 and states.count("replaced") == 2:
                break
            time.sleep(0.2)
        assert sorted(states) == ["live", "replaced", "replaced"], states
        assert len(streamer_pids(handle["display"])) == 1
    finally:
        for ctx in contexts:
            ctx.close()


def test_a_held_key_is_released_when_the_viewer_is_replaced(browser, server, probe):
    _, log = probe
    first_ctx, first = _player(browser, server)
    second_ctx = None
    try:
        wait_live(first)
        first.keyboard.down("z")
        wait_for_lines(log, ["keydown z"])
        second_ctx, second = _player(browser, server)
        wait_live(second)
        wait_for_lines(log, ["keydown z", "keyup z"])
    finally:
        first_ctx.close()
        if second_ctx:
            second_ctx.close()


def _key(page, kind, key, code, shift=False):
    page.evaluate(
        """([kind, key, code, shift]) => document.getElementById('player').dispatchEvent(
            new KeyboardEvent(kind, {key, code, shiftKey: shift, bubbles: true}))""",
        [kind, key, code, shift],
    )


def test_layout_symbols_arrive_as_typed(browser, server, probe):
    """A French keyboard: unshifted '&' and Shift+'1' on a US display keymap."""
    _, log = probe
    context, page = _player(browser, server)
    try:
        wait_live(page)
        _key(page, "keydown", "&", "Digit1")
        _key(page, "keyup", "&", "Digit1")
        # The press carries Shift (it is Shift+7 on the US keymap); the
        # release is the same physical key, seen without Shift.
        wait_for_lines(log, ["keydown ampersand", "keyup 7"])
        _key(page, "keydown", "Shift", "ShiftLeft", shift=True)
        _key(page, "keydown", "1", "Digit1", shift=True)
        _key(page, "keyup", "1", "Digit1", shift=True)
        _key(page, "keyup", "Shift", "ShiftLeft")
        lines = wait_for_lines(log, ["keydown 1", "keyup Shift_L"])
        assert "keydown exclam" not in lines
    finally:
        context.close()


def test_shift_stays_on_navigation_keys(browser, server, probe):
    """Shift+Right selects and Shift+Tab goes back: the modifier must reach
    the app on non-printable keys (only printable symbols get Shift fixed)."""
    _, log = probe
    context, page = _player(browser, server)
    try:
        wait_live(page)
        _key(page, "keydown", "Shift", "ShiftLeft", shift=True)
        _key(page, "keydown", "ArrowRight", "ArrowRight", shift=True)
        _key(page, "keyup", "ArrowRight", "ArrowRight", shift=True)
        _key(page, "keydown", "Tab", "Tab", shift=True)
        _key(page, "keyup", "Tab", "Tab", shift=True)
        _key(page, "keyup", "Shift", "ShiftLeft")
        # Shift+Tab is ISO_Left_Tab on X: the press carried Shift.
        wait_for_lines(
            log, ["state Right shift", "state ISO_Left_Tab shift", "keyup Shift_L"]
        )
    finally:
        context.close()


def _order(lines, prefixes):
    return [
        next(i for i, line in enumerate(lines) if line.startswith(p)) for p in prefixes
    ]


def test_mouse_button_chords(browser, server, probe):
    """Left down, right down, left up, right up: each change reaches the app."""
    _, log = probe
    context, page = _player(browser, server)
    try:
        wait_live(page)
        seen = len(log.read_text().splitlines())  # the module shares one probe
        page.mouse.move(400, 300)
        page.mouse.down(button="left")
        page.mouse.down(button="right")
        page.mouse.up(button="left")
        page.mouse.up(button="right")
        lines = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            lines = log.read_text().splitlines()[seen:]
            if any(line.startswith("btnup 3 ") for line in lines):
                break
            time.sleep(0.1)
        order = _order(lines, ["btndown 1 ", "btndown 3 ", "btnup 1 ", "btnup 3 "])
        assert order == sorted(order), lines
    finally:
        context.close()


def _xdotool_typing_on(display: str) -> list[int]:
    """`xdotool type` processes whose DISPLAY is ``display``."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            env = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv[:2] == [b"xdotool", b"type"] and f"DISPLAY={display}".encode() in env:
            found.append(int(entry.name))
    return found


def _text(log: Path) -> str:
    return log.read_text() if log.exists() else ""


def test_a_long_paste_stops_with_its_viewer(browser, server, probe):
    handle, log = probe
    first_ctx, first = _player(browser, server)
    second_ctx = None
    try:
        wait_live(first)
        first.evaluate(
            "window.MerlinPlayer.stream.send({t: 'text', s: 'q'.repeat(400)})"
        )
        deadline = time.monotonic() + 10
        while _text(log).count("keydown q") < 20:
            assert time.monotonic() < deadline, "typing never started"
            time.sleep(0.05)
        assert _xdotool_typing_on(handle["display"])
        second_ctx, second = _player(browser, server)
        wait_live(second)
        # The typist is gone with its viewer...
        assert _xdotool_typing_on(handle["display"]) == []
        # ...and once the probe has drained its queue, the paste stopped short.
        deadline = time.monotonic() + 15
        typed = -1
        while time.monotonic() < deadline:
            now = _text(log).count("keydown q")
            if now == typed:
                break
            typed = now
            time.sleep(1)
        assert typed < 400, "the whole paste went through"
    finally:
        first_ctx.close()
        if second_ctx:
            second_ctx.close()


def test_keys_after_a_paste_wait_for_it(browser, server, probe):
    """A paste then Enter: Enter must arrive after the whole paste."""
    _, log = probe
    context, page = _player(browser, server)
    try:
        wait_live(page)
        seen = len(_text(log).splitlines())
        page.evaluate(
            """() => {
                const s = window.MerlinPlayer.stream;
                s.send({t: 'text', s: 'hello'});
                s.send({t: 'key', k: 'Return', d: true});
                s.send({t: 'key', k: 'Return', d: false});
            }"""
        )
        deadline = time.monotonic() + 10
        lines: list[str] = []
        while time.monotonic() < deadline:
            lines = _text(log).splitlines()[seen:]
            if "keyup Return" in lines:
                break
            time.sleep(0.05)
        typed = [line.split()[1] for line in lines if line.startswith("keydown ")]
        assert typed == ["h", "e", "l", "l", "o", "Return"], typed
    finally:
        context.close()


def test_a_killed_streamer_takes_its_typist_with_it(browser, server, probe):
    handle, log = probe
    context, page = _player(browser, server)
    try:
        wait_live(page)
        page.evaluate(
            "window.MerlinPlayer.stream.send({t: 'text', s: 'w'.repeat(400)})"
        )
        deadline = time.monotonic() + 10
        while _text(log).count("keydown w") < 20:
            assert time.monotonic() < deadline, "typing never started"
            time.sleep(0.05)
        assert _xdotool_typing_on(handle["display"])
        [streamer] = streamer_pids(handle["display"])
        os.kill(streamer, signal.SIGKILL)  # no cleanup code runs
        deadline = time.monotonic() + 5
        while _xdotool_typing_on(handle["display"]):
            assert time.monotonic() < deadline, "the typist outlived its streamer"
            time.sleep(0.05)
        deadline = time.monotonic() + 15
        typed = -1
        while time.monotonic() < deadline:
            now = _text(log).count("keydown w")
            if now == typed:
                break
            typed = now
            time.sleep(1)
        assert typed < 400
    finally:
        context.close()
