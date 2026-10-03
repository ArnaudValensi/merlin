"""Streaming on a bad network: loss and delay injected with ``tc netem`` in an
unprivileged network namespace (``unshare -rn``), the whole stack inside
(see netem_session.py). Checks the diagnostics, the chip, the rate control
and the keyframes on loss, then the recovery."""

import json
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


@pytest.fixture(scope="module")
def session() -> dict:
    result = subprocess.run(
        ["unshare", "-rn", sys.executable, str(HERE / "netem_session.py")],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def _rates(log: list[str]) -> list[int]:
    return [
        int(m.group(1)) for line in log if (m := re.match(r"rate: (\d+) kbit/s", line))
    ]


def test_the_route_is_logged_and_shown(session):
    assert session["launch"] == "running"
    assert any(
        line.startswith("ice: route LAN: 10.99.0.1") for line in session["log"]
    ), session["log"]
    assert all(text.startswith("LAN") for _, text, _, _ in session["chips"])


def test_loss_brings_the_rate_down_and_the_gauge_with_it(session):
    rates = _rates(session["log"])
    assert rates and min(rates) <= 0.5 * 3555, rates  # 1280x720's ceiling
    lossy = [level for phase, _, level, _ in session["chips"] if phase == "lossy"]
    assert min(lossy) <= 2, lossy
    # The delay shows in the chip's round trip (60 ms each way).
    rtts = [
        int(m.group(1))
        for phase, text, _, _ in session["chips"]
        if phase == "lossy" and (m := re.search(r"· (\d+) ms", text))
    ]
    assert rtts and max(rtts) >= 100, rtts


def test_lost_frames_get_keyframes(session):
    summaries = [line for line in session["log"] if line.startswith("session: ")]
    assert len(summaries) == 1, session["log"]
    m = re.search(r"(\d+) keyframe requests \((\d+) forced\)", summaries[0])
    assert m, summaries[0]
    requests, forced = int(m.group(1)), int(m.group(2))
    # 15 % loss on a moving picture: the browser asks for keyframes, and
    # webrtcbin hands each request (or a burst of them, coalesced) to the
    # encoder as a force-key-unit event.
    assert requests >= 1, f"no keyframe request under loss: {summaries[0]}"
    assert forced >= 1, f"the encoder never heard the requests: {summaries[0]}"


def test_the_rate_climbs_back_and_the_stream_never_dropped(session):
    rates = _rates(session["log"])
    lowest = rates.index(min(rates))
    assert max(rates[lowest:]) > min(rates), rates
    recover = [level for phase, _, level, _ in session["chips"] if phase == "recover"]
    assert recover[-1] > min(recover), recover
    assert all(state == "live" for *_, state in session["chips"]), session["chips"]
