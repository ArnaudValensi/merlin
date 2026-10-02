"""WebRTC streaming of an app to a real browser, on this machine's network.

The browser picks the codec (H.264 when it can decode it, NVENC on an NVIDIA
machine); a page that only advertises VP8 exercises the software fallback.
"""

import shutil
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
        lines = wait_for_lines(log, ["btndown 1 300 200", "keydown x", "keyup x"])
        assert "btndown 1 300 200" in lines
        assert "keydown x" in lines
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
