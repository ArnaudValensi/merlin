"""Streaming on a bad network: loss and delay injected with ``tc netem`` in an
unprivileged network namespace (``unshare -rn``), the whole stack inside
(see netem_session.py). Checks the diagnostics, the chip, the rate control
and the keyframes on loss, then the recovery."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app_stream_support import requires_streaming

HERE = Path(__file__).resolve().parent


def _namespace_netem_works() -> bool:
    if not (shutil.which("unshare") and shutil.which("tc") and shutil.which("ip")):
        return False
    probe = (
        "ip link set lo up && ip link add d0 type dummy && "
        "tc qdisc add dev lo root netem loss 1%"
    )
    return (
        subprocess.run(
            ["unshare", "-rn", "sh", "-c", probe], capture_output=True, check=False
        ).returncode
        == 0
    )


pytestmark = [
    requires_streaming,
    pytest.mark.skipif(
        not _namespace_netem_works(),
        reason="needs unprivileged network namespaces, dummy links and tc netem",
    ),
    pytest.mark.skipif(
        shutil.which("gst-launch-1.0") is None, reason="needs gst-launch-1.0"
    ),
]


ENCODERS = ("nvh264enc", "openh264enc", "vp8enc")


@pytest.fixture(scope="module", params=ENCODERS)
def session(request) -> dict:
    """One bad-network session per encoder (each one must repair loss)."""
    result = subprocess.run(
        ["unshare", "-rn", sys.executable, str(HERE / "netem_session.py")],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "NETEM_ENCODER": request.param},
    )
    assert result.returncode == 0, result.stderr[-3000:]
    data = json.loads(result.stdout.strip().splitlines()[-1])
    if data.get("encoder") != request.param:
        pytest.skip(f"{request.param} is not usable here (got {data.get('encoder')})")
    return data


def _rates(log: list[str]) -> list[int]:
    return [
        int(m.group(1)) for line in log if (m := re.match(r"rate: (\d+) kbit/s", line))
    ]


def test_the_route_is_logged_and_shown(session):
    assert session["launch"] == "running"
    assert any(
        line.startswith("ice: route LAN: 10.99.0.1") for line in session["log"]
    ), session["log"]
    assert all(text.startswith("LAN") for _, text, *_ in session["chips"])


def test_loss_brings_the_rate_down_and_the_gauge_with_it(session):
    rates = _rates(session["log"])
    assert rates and min(rates) <= 0.5 * 3555, rates  # 1280x720's ceiling
    lossy = [level for phase, _, level, *_ in session["chips"] if phase == "lossy"]
    assert min(lossy) <= 2, lossy
    # The delay shows in the chip's round trip (60 ms each way).
    rtts = [
        int(m.group(1))
        for phase, text, *_ in session["chips"]
        if phase == "lossy" and (m := re.search(r"· (\d+) ms", text))
    ]
    assert rtts and max(rtts) >= 100, rtts


def test_lost_frames_get_keyframes(session):
    summaries = [line for line in session["log"] if line.startswith("session: ")]
    assert len(summaries) == 1, session["log"]
    m = re.search(
        r"(\d+) keyframe requests \((\d+) to the encoder, (\d+) answered\)",
        summaries[0],
    )
    assert m, summaries[0]
    requests, forwarded, answered = (int(g) for g in m.groups())
    if requests == 0:
        # The browser repaired the loss without asking (VP8's error-resilient
        # partitions often allow it): nothing to serve in this run. The
        # picture test still requires frames shown under loss and after.
        pytest.skip(f"no keyframe request in this run: {summaries[0]}")
    # webrtcbin hands each request (or a burst of them, coalesced) to the
    # encoder, and the encoder answers with a keyframe within half a second.
    assert forwarded >= 1, f"the encoder never heard the requests: {summaries[0]}"
    assert answered >= 1, f"no keyframe answered a request: {summaries[0]}"


def test_the_rate_climbs_back_and_the_stream_never_dropped(session):
    rates = _rates(session["log"])
    lowest = rates.index(min(rates))
    assert max(rates[lowest:]) > min(rates), rates
    recover = [level for phase, _, level, *_ in session["chips"] if phase == "recover"]
    assert recover[-1] > min(recover), recover
    assert all(state == "live" for _, _, _, state, _ in session["chips"]), session[
        "chips"
    ]


def _shown_rate(samples: list) -> float:
    """Frames actually shown per second between the first and last sample
    (total minus dropped: the total counts dropped frames too)."""
    (total0, dropped0, t0), (total1, dropped1, t1) = samples[0], samples[-1]
    shown = (total1 - dropped1) - (total0 - dropped0)
    return shown / max(t1 - t0, 0.001)


def _check_picture(chips: list) -> None:
    frames: dict = {"clean": [], "lossy": [], "recover": []}
    for phase, _, _, _, sample in chips:
        frames[phase].append(sample)
    assert _shown_rate(frames["lossy"]) > 0, f"frozen under loss: {frames['lossy']}"
    rate = _shown_rate(frames["recover"][-6:])
    assert rate >= 20, f"{rate:.1f} frames/s shown after the loss: {frames['recover']}"


def test_the_picture_keeps_moving_and_moves_freely_after(session):
    """The connection staying up is not enough: frames must keep being shown,
    and after the loss the picture runs at full pace again."""
    _check_picture(session["chips"])


def test_the_picture_check_does_not_count_dropped_frames():
    """Frames that arrive but are all dropped are a frozen picture."""

    def chips(dropping: bool) -> list:
        out = []
        for i in range(10):
            phase = "lossy" if i < 4 else "recover"
            total = 60 * i
            out.append(
                [phase, "", 4, "live", [total, total if dropping else 0, float(i)]]
            )
        return out

    _check_picture(chips(dropping=False))
    with pytest.raises(AssertionError, match="frozen under loss"):
        _check_picture(chips(dropping=True))
