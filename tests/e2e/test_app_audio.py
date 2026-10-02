"""The app's sound in the stream: a tone heard by a real browser, the sound
controls of each surface, and a picture that outlives its sound.

Needs PipeWire (the sinks and the capture) besides the streaming stack; the
tone is a 440 Hz sine the probe plays through ``pulsesink``.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

from app import sessions  # noqa: E402
from app_stream_support import (  # noqa: E402
    APP_OPTIONS,
    cli,
    launch_probe,
    requires_streaming,
    stop_all,
    wait_live,
)


def _gst_has(*names: str) -> bool:
    return all(
        subprocess.run(
            ["gst-inspect-1.0", "--exists", name], capture_output=True, check=False
        ).returncode
        == 0
        for name in names
    )


MERLIN_OPTIONS = APP_OPTIONS
pytestmark = [
    requires_streaming,
    pytest.mark.skipif(
        not (
            sessions.audio_available()
            and _gst_has("pipewiresrc", "opusenc", "rtpopuspay", "pulsesink")
        ),
        reason="needs PipeWire and the GStreamer pipewire/opus elements",
    ),
]

SHOTS = Path("/tmp/merlin-app-shots")

# The loudest frequency on the element's stream over a few seconds (WebAudio).
LISTEN = """async (sel) => {
    const video = document.querySelector(sel);
    const ctx = new AudioContext();
    await ctx.resume();
    const source = ctx.createMediaStreamSource(video.srcObject);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 8192;
    source.connect(analyser);
    const bins = new Float32Array(analyser.frequencyBinCount);
    let best = {db: -Infinity, hz: 0};
    const end = performance.now() + 3000;
    while (performance.now() < end) {
        await new Promise((r) => setTimeout(r, 200));
        analyser.getFloatFrequencyData(bins);
        for (let i = 1; i < bins.length; i++) {
            if (bins[i] > best.db) best = {db: bins[i], hz: i * ctx.sampleRate / analyser.fftSize};
        }
    }
    await ctx.close();
    return best;
}"""


@pytest.fixture(scope="module")
def playwright():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright):
    # WebAudio may run without a click: the analyser is the test's ear.
    browser = playwright.chromium.launch(
        args=["--autoplay-policy=no-user-gesture-required"]
    )
    yield browser
    browser.close()


@pytest.fixture(scope="module")
def strict_browser(playwright):
    """Chromium with its real autoplay rule: sound only after a gesture."""
    browser = playwright.chromium.launch(
        args=["--autoplay-policy=document-user-activation-required"]
    )
    yield browser
    browser.close()


@pytest.fixture(autouse=True)
def _clean(merlin):
    yield
    stop_all(merlin)


def _tone(merlin, tmp_path, name="tone", *extra, env=None) -> dict:
    return launch_probe(
        merlin,
        tmp_path / f"{name}.log",
        name,
        *extra,
        env=env,
        probe_args=("--tone", "440"),
    )


def _record(merlin, name: str) -> dict:
    """The app's full state record (the CLI shows only the public part)."""
    path = Path(merlin.home) / "data" / "apps" / "sessions" / f"{name}.json"
    return json.loads(path.read_text())


def _unload_sink(record: dict) -> None:
    subprocess.run(["pactl", "unload-module", str(record["audio_module"])], check=True)


def _capture_links() -> list[str]:
    """What the streamer's capture is linked to (PipeWire graph)."""
    dump = json.loads(
        subprocess.run(["pw-dump"], capture_output=True, text=True, check=True).stdout
    )
    nodes = {o["id"]: o for o in dump if o.get("type") == "PipeWire:Interface:Node"}

    def name(node_id):
        return nodes.get(node_id, {}).get("info", {}).get("props", {}).get("node.name")

    return [
        name(o["info"]["output-node-id"])
        for o in dump
        if o.get("type") == "PipeWire:Interface:Link"
        and name(o["info"]["input-node-id"]) == "merlin-app-stream"
    ]


def _open_player(browser, server, name, **context_args):
    context = browser.new_context(**context_args)
    page = context.new_page()
    page.goto(f"{server}/apps/{name}/play")
    wait_live(page)
    return context, page


def test_the_browser_hears_the_app(merlin, browser, server, tmp_path):
    handle = _tone(merlin, tmp_path)
    assert handle["audio"] == "stream"
    context, page = _open_player(browser, server, "tone")
    try:
        page.wait_for_selector("#player[data-audio='1']")
        page.wait_for_function(
            "document.getElementById('player-video').srcObject.getAudioTracks().length === 1"
        )
        heard = page.evaluate(LISTEN, "#player-video")
        assert abs(heard["hz"] - 440) < 12, heard
        assert heard["db"] > -60, heard
        # Only the app's sink feeds the capture.
        record = _record(merlin, "tone")
        links = _capture_links()
        assert links and set(links) == {record["audio_sink"]}, links
    finally:
        context.close()


def test_an_app_playing_locally_streams_the_picture_only(
    merlin, browser, server, tmp_path
):
    handle = _tone(merlin, tmp_path, "local", "--audio", "local")
    assert handle["audio"] == "local"
    context, page = _open_player(browser, server, "local")
    try:
        page.wait_for_selector("#player[data-audio='0']")
        assert (
            page.evaluate(
                "document.getElementById('player-video').srcObject.getAudioTracks().length"
            )
            == 0
        )
        assert page.locator("[data-action='sound']").is_hidden()
    finally:
        context.close()


def test_the_player_starts_muted_and_the_first_tap_brings_the_sound(
    merlin, playwright, strict_browser, server, tmp_path
):
    _tone(merlin, tmp_path, "tone", "--controls", "touch")
    phone = playwright.devices["Pixel 7 landscape"]
    context, page = _open_player(strict_browser, server, "tone", **phone)
    try:
        page.wait_for_selector("#player[data-audio='1']")
        video = "document.getElementById('player-video')"
        assert page.evaluate(f"{video}.muted") is True
        page.locator("#player-video").tap()
        page.wait_for_function(f"!{video}.muted")
        page.wait_for_function(f"!{video}.paused")

        page.tap("#player-menu-btn")
        item = page.locator("[data-action='sound']")
        assert item.is_visible()
        assert item.inner_text().split() == ["Sound", "On"]
        SHOTS.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOTS / "player-sound-sheet.png"))
        item.tap()
        page.wait_for_function(f"{video}.muted")
        # The label follows the element's volumechange event.
        page.wait_for_function(
            "document.getElementById('player-sound-state').textContent === 'Off'"
        )
        assert page.evaluate("localStorage.getItem('app-sound-player')") == "0"

        # Remembered: a reload and a tap leave it off.
        page.reload()
        wait_live(page)
        page.locator("#player-video").tap()
        page.wait_for_timeout(300)
        assert page.evaluate(f"{video}.muted") is True
    finally:
        context.close()


def _terminal(browser, server, **context_args):
    context = browser.new_context(**context_args)
    page = context.new_page()
    page.goto(f"{server}/terminal")
    page.wait_for_function(
        "() => window.MerlinTerminal && window.MerlinTerminal.currentWindow()",
        timeout=20000,
    )
    return context, page


def _launch_from(merlin, page, tmp_path) -> dict:
    window = page.evaluate("window.MerlinTerminal.currentWindow()")
    pane = subprocess.run(
        ["tmux", "list-panes", "-t", window, "-F", "#{pane_id}"],
        env=merlin.env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    return _tone(merlin, tmp_path, env={"TMUX_PANE": pane})


def test_the_docked_panel_plays_the_sound(merlin, strict_browser, server, tmp_path):
    context, page = _terminal(
        strict_browser, server, viewport={"width": 1400, "height": 820}
    )
    try:
        _launch_from(merlin, page, tmp_path)
        page.wait_for_selector("#app-btn:not([hidden])", timeout=10000)
        page.click("#app-btn")
        wait_live(page, ".app-panel-body")
        speaker = page.locator("#app-panel .app-sound")
        speaker.wait_for(state="visible")
        video = "document.querySelector('.app-panel-video')"
        # The click that opened the panel is the gesture: sound on, playing.
        assert page.evaluate(f"{video}.muted") is False
        page.wait_for_function(f"!{video}.paused")
        assert speaker.get_attribute("aria-pressed") == "true"
        SHOTS.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOTS / "terminal-sound-desktop.png"))
        speaker.click()
        assert page.evaluate(f"{video}.muted") is True
        page.wait_for_function(
            "document.querySelector('#app-panel .app-sound')"
            ".getAttribute('aria-pressed') === 'false'"
        )
        assert page.evaluate("localStorage.getItem('app-sound-panel')") == "0"
    finally:
        context.close()


def test_the_mini_player_stays_quiet_until_asked(
    merlin, playwright, strict_browser, server, tmp_path
):
    context, page = _terminal(strict_browser, server, **playwright.devices["Pixel 7"])
    try:
        _launch_from(merlin, page, tmp_path)
        page.wait_for_selector("#app-btn:not([hidden])", timeout=10000)
        page.tap("#app-btn")
        page.wait_for_selector(".app-panel.mini")
        wait_live(page, ".app-panel-body")
        speaker = page.locator("#app-panel .app-sound")
        speaker.wait_for(state="visible")
        video = "document.querySelector('.app-panel-video')"
        assert page.evaluate(f"{video}.muted") is True
        SHOTS.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOTS / "terminal-sound-mobile.png"))
        speaker.tap()
        page.wait_for_function(f"!{video}.muted")
        assert page.url.endswith("/terminal"), "the speaker is not a tap on the player"
        assert page.evaluate("localStorage.getItem('app-sound-mini')") == "1"
    finally:
        context.close()


def test_the_picture_outlives_a_lost_sink(merlin, browser, server, tmp_path):
    _tone(merlin, tmp_path)
    context, page = _open_player(browser, server, "tone")
    try:
        page.wait_for_selector("#player[data-audio='1']")
        record = _record(merlin, "tone")
        _unload_sink(record)
        before = page.evaluate("document.getElementById('player-video').currentTime")
        time.sleep(3)
        assert (
            page.evaluate("document.getElementById('player').dataset.streamState")
            == "live"
        )
        after = page.evaluate("document.getElementById('player-video').currentTime")
        assert after - before > 2, "the picture kept playing"
        # The capture was not moved to another device (microphone, speakers).
        assert _capture_links() == []
    finally:
        context.close()


def test_a_sink_gone_before_the_viewer_gives_a_silent_stream(
    merlin, browser, server, tmp_path
):
    _tone(merlin, tmp_path)
    _unload_sink(_record(merlin, "tone"))
    context, page = _open_player(browser, server, "tone")
    try:
        page.wait_for_selector("#player[data-audio='0']")
        assert _capture_links() == []
    finally:
        context.close()


def test_stop_and_exit_leave_no_sink(merlin, tmp_path):
    _tone(merlin, tmp_path)
    sink = _record(merlin, "tone")["audio_sink"]
    assert cli(merlin, "stop", "tone").returncode == 0
    sinks = subprocess.run(
        ["pactl", "list", "short", "sinks"], capture_output=True, text=True, check=True
    ).stdout
    assert sink not in sinks
