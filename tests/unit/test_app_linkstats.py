"""The stream's link: routes, candidates, stats snapshots, rate control and
the session summary (app/linkstats.py, pure)."""

import pytest

from app import linkstats
from app.linkstats import RateControl


@pytest.mark.parametrize(
    ("address", "kind", "route"),
    [
        ("192.168.1.12", "host", "LAN"),
        ("10.1.2.3", "host", "LAN"),
        ("172.20.0.5", "prflx", "LAN"),
        ("127.0.0.1", "host", "LAN"),
        ("fe80::c45c:5212:4029:56d5", "host", "LAN"),
        ("fd12:3456::1", "host", "LAN"),
        ("100.101.102.103", "host", "Tailscale"),
        ("fd7a:115c:a1e0:ab12::1", "host", "Tailscale"),
        ("2001:861:61c0:8770:a065:d2fb:61b8:6724", "host", "Internet · IPv6"),
        ("176.186.26.141", "srflx", "Internet · IPv4"),
        ("176.186.26.141", "relay", "Relay"),
        ("abcd-1234.local", "host", "?"),
    ],
)
def test_routes_are_named_from_the_far_address(address, kind, route):
    assert linkstats.classify(address, kind) == route


@pytest.mark.parametrize(
    ("remote", "local", "route"),
    [
        ("2001:861:61c0:8770::5", "2001:861:61c0:8770:a065:d2fb:61b8:6724", "LAN"),
        (
            "2001:861:61c0:9999::5",
            "2001:861:61c0:8770:a065:d2fb:61b8:6724",
            "Internet · IPv6",
        ),
        ("2a01:cb00::9", "2001:861:61c0:8770::1", "Internet · IPv6"),
        ("82.64.10.20", "82.64.10.7", "LAN"),  # a public /24 of our own
        ("82.64.11.20", "82.64.10.7", "Internet · IPv4"),
        ("82.64.10.20", "2001:861::1", "Internet · IPv4"),  # other family
        ("192.168.1.30", "192.168.1.12", "LAN"),
    ],
)
def test_our_own_end_tells_a_public_lan_from_the_internet(remote, local, route):
    assert linkstats.classify(remote, "prflx", local) == route


def test_candidates_are_reduced_to_type_protocol_and_family():
    d = linkstats.describe_candidate
    assert (
        d("candidate:1 1 UDP 2015363327 2001:861::1 46569 typ host") == "host udp ipv6"
    )
    assert (
        d("candidate:4 1 UDP 2015363583 192.168.1.12 40137 typ host") == "host udp ipv4"
    )
    assert (
        d(
            "candidate:9 1 udp 2113937151 0e1f2a3b-aaaa.local 51234 typ host generation 0"
        )
        == "host udp mdns"
    )
    assert (
        d("candidate:7 1 tcp 1518280447 1.2.3.4 9 typ srflx raddr 0.0.0.0 rport 0")
        == "srflx tcp ipv4"
    )
    assert d("garbage") == "unparsed"


def _stats(*, report=100, fraction=0.0, rtt=0.05, lost=0, sent=1000, sent_bytes=10**6):
    """One get-stats reply as the streamer converts it (the shapes webrtcbin
    gives on GStreamer 1.28)."""
    return [
        {"_name": "peer-connection", "id": "pc"},
        {
            "_name": "local-candidate",
            "id": "L",
            "address": "2001:861::1",
            "port": 46569,
            "candidate-type": "host",
            "protocol": "udp",
        },
        {
            "_name": "remote-candidate",
            "id": "R",
            "address": "2a01:cb00::9",
            "port": 51234,
            "candidate-type": "prflx",
            "protocol": "udp",
        },
        {
            "_name": "candidate-pair",
            "id": "P",
            "local-candidate-id": "L",
            "remote-candidate-id": "R",
        },
        {"_name": "transport", "id": "T", "selected-candidate-pair-id": "P"},
        {
            "_name": "outbound-rtp",
            "id": "O",
            "kind": "video",
            "bytes-sent": sent_bytes,
            "packets-sent": sent,
            "pli-count": 2,
            "fir-count": 1,
        },
        {"_name": "outbound-rtp", "id": "OA", "kind": "audio", "packets-sent": 5},
        {
            "_name": "remote-inbound-rtp",
            "id": "RI",
            "kind": "video",
            "packets-lost": lost,
            "fraction-lost": fraction,
            "round-trip-time": rtt,
            "gst-rtpsource-stats": {"have-rb": True, "rb-exthighestseq": report},
        },
    ]


def test_a_snapshot_reads_the_route_the_report_and_the_counters():
    snap = linkstats.read_snapshot(_stats(fraction=0.04, rtt=0.048, lost=7))
    assert snap.route == "Internet · IPv6"
    assert snap.local == "2001:861::1 46569 host"
    assert snap.remote == "2a01:cb00::9 51234 prflx"
    assert snap.protocol == "udp"
    assert snap.report == 100
    assert snap.fraction_lost == pytest.approx(0.04)
    assert snap.rtt == pytest.approx(0.048)
    assert (snap.packets_lost, snap.packets_sent) == (7, 1000)
    assert snap.keyframe_requests == 3  # PLI + FIR, video only


def test_no_receiver_report_yet_means_no_report():
    stats = _stats()
    stats[-1]["gst-rtpsource-stats"] = {"have-rb": False}
    stats[-1]["packets-lost"] = -1
    snap = linkstats.read_snapshot(stats)
    assert snap.report is None and snap.fraction_lost is None
    assert snap.packets_lost == 0


def test_a_zero_round_trip_is_unknown():
    assert linkstats.read_snapshot(_stats(rtt=0.0)).rtt is None


def test_a_clean_link_stays_at_the_ceiling():
    rc = RateControl(ceiling=5500)
    for _ in range(20):
        assert rc.update(0.0, 0.03) == 5500
    assert rc.level == 4


def test_loss_steps_down_in_proportion():
    rc = RateControl(ceiling=5000)
    assert rc.update(0.05, None) == 4500  # some loss: x0.9
    assert rc.update(0.20, None) == 3150  # heavy loss: x0.7
    assert rc.update(0.01, None) == 3150  # clean once: hold
    assert rc.update(0.01, None) == 3150  # twice
    assert rc.update(0.01, None) == 3402  # three clean in a row: x1.08
    assert rc.update(0.01, None) == 3674  # and on each one after


def test_a_swelling_round_trip_backs_off():
    rc = RateControl(ceiling=5000)
    rc.update(0.0, 0.040)  # the best round trip so far
    assert rc.update(0.0, 0.070) == 5000  # 1.75x: hold
    assert rc.update(0.0, 0.090) == 3500  # over 2x the best: x0.7
    assert rc.update(0.0, 0.050) == 3500  # within 1.3x: one clean report


def test_a_latency_that_rose_for_good_becomes_the_new_baseline():
    """A phone moving to a farther cell: the old best trip ages out of the
    window and the rate climbs back instead of staying at the floor."""
    rc = RateControl(ceiling=5000)
    rc.update(0.0, 0.030)
    for _ in range(40):
        rc.update(0.0, 0.120)  # 4x the old best, for good
    assert rc.rate > rc.floor
    for _ in range(40):
        rc.update(0.0, 0.120)
    assert rc.rate == 5000


def test_a_lan_round_trip_that_doubles_is_not_congestion():
    """Sub-millisecond trips vary by multiples: the baseline is 25 ms at least."""
    rc = RateControl(ceiling=5000)
    for rtt in (0.0003, 0.0011, 0.004, 0.0002, 0.03, 0.049):
        assert rc.update(0.0, rtt) == 5000
    assert rc.update(0.0, 0.051) == 3500  # over 2x the 25 ms baseline


def test_floor_and_ceiling_hold():
    rc = RateControl(ceiling=5500)
    assert rc.floor == 600
    for _ in range(30):
        rc.update(0.5, None)
    assert rc.rate == 600
    assert rc.level == 1
    big = RateControl(ceiling=12000)
    assert big.floor == 1200  # 10 % of the ceiling when that is more
    for _ in range(200):
        rc.update(0.0, None)
    assert rc.rate == 5500


@pytest.mark.parametrize(
    ("rate", "level"),
    [(5500, 4), (4675, 4), (4674, 3), (3300, 3), (3299, 2), (1925, 2), (1924, 1)],
)
def test_the_gauge_levels(rate, level):
    rc = RateControl(ceiling=5500, rate=rate)
    assert rc.level == level


def test_the_session_summary():
    session = linkstats.Session()
    assert session.summary(10.0) == "session: never connected"
    session.connected_at = 100.0
    session.first = linkstats.read_snapshot(_stats(sent_bytes=1_000_000))
    session.last = linkstats.read_snapshot(
        _stats(sent=2000, lost=20, sent_bytes=31_000_000)
    )
    session.rates = [5500, 3850, 4158]
    session.forwarded = 3
    session.answered = 2
    line = session.summary(160.0)
    assert line == (
        "session: 1m00s via Internet · IPv6, sent 4000 kbit/s on average, "
        "rate 3850-5500 kbit/s, loss 1.0 %, 3 keyframe requests "
        "(3 to the encoder, 2 answered)"
    )
