"""Reaching the machine from another network, across a small internet of
network namespaces (nat_session.py, inside ``unshare -rn``): STUN punching
through two NATs, the relay (TURN) when the phone's NAT changes ports per
destination, and the relay forced with ?ice=relay. Each case checks the
path the streamer reports, the chip, its detail, and frames decoded."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app_stream_support import requires_streaming

HERE = Path(__file__).resolve().parent


def _small_internet_works() -> bool:
    tools = ("unshare", "nsenter", "ip", "iptables", "turnserver", "gst-launch-1.0")
    if not all(shutil.which(t) for t in tools):
        return False
    probe = (
        "ip link set lo up && "
        "iptables -t nat -A POSTROUTING -j MASQUERADE --random-fully && "
        "iptables -A FORWARD -m conntrack --ctstate ESTABLISHED -j ACCEPT && "
        "unshare --net true"
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
        not _small_internet_works(),
        reason="needs unprivileged namespaces, iptables NAT and conntrack (the "
        "xt_MASQUERADE and xt_conntrack modules loaded: Docker loads them), "
        "gst-launch-1.0 and coturn's turnserver",
    ),
]

# case: (the phone's NAT, the page's query, the portal offers the relay)
CASES = {
    "stun": ("cone", "", False),  # no relay at all: STUN must punch through
    "turn": ("symmetric", "", True),  # a new port per destination: relay only
    "relay": ("cone", "?ice=relay", True),  # the test switch
}
PATH = {"stun": "STUN", "turn": "TURN", "relay": "TURN"}


@pytest.fixture(scope="module", params=list(CASES))
def session(request) -> dict:
    nat, query, relay = CASES[request.param]
    cmd = ["unshare", "-rn", sys.executable, str(HERE / "nat_session.py")]
    cmd += ["--phone-nat", nat, "--query", query]
    if not relay:
        cmd.append("--no-relay")
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=300, check=False
    )
    assert result.returncode == 0, result.stderr[-3000:]
    data = json.loads(result.stdout.strip().splitlines()[-1])
    data["case"] = request.param
    return data


def test_the_stream_takes_the_expected_path(session):
    want = PATH[session["case"]]
    paths = [line for line in session["log"] if line.startswith("ice: path ")]
    assert paths and paths[-1].startswith(f"ice: path {want} ("), session["log"]
    # Never relay to relay: production's relay refuses its own address.
    assert not ("local relay" in paths[-1] and "remote relay" in paths[-1]), paths
    assert session["chip"].startswith(f"Internet · IPv4 · {want} · "), session["chip"]
    summary = [line for line in session["log"] if line.startswith("session: ")]
    assert summary and f"via Internet · IPv4 · {want}," in summary[-1], summary


def test_the_picture_moves_through_it(session):
    decoded = [row[2] for row in session.get("inbound") or [] if row]
    assert len(decoded) >= 4, session
    per_second = (decoded[-1] - decoded[0]) / (len(decoded) - 1)
    assert per_second >= 20, decoded


def test_the_detail_says_what_each_way_gave(session):
    reach = session["detail"].splitlines()[-1]
    turn = "TURN no access" if session["case"] == "stun" else "TURN ok"
    assert reach == f"STUN ok 82.66.10.2 · UPnP no router · {turn}", reach
    servers = [line for line in session["log"] if line.startswith("ice: servers ")]
    expected = (
        "ice: servers stun (turn: no access)"
        if session["case"] == "stun"
        else "ice: servers stun, turn"
    )
    assert servers == [expected], session["log"]
