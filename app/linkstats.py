"""What the stream's network link is doing, and how hard to push it.

Standard library only: the streamer (system Python) imports it next to it,
the tests import it from the package. Nothing here talks to GStreamer: the
streamer turns ``webrtcbin``'s stats into plain dicts, and this module reads
them.

- ``classify``: names a route from the far end's address (LAN, Tailscale,
  Internet), the same rule as the browser's chip (client.js ``routeOf``).
- ``describe_candidate``: an ICE candidate line, reduced to its type,
  protocol and address family (for the log).
- ``read_snapshot``: the figures that matter from one stats reply.
- ``RateControl``: the bitrate controller (AIMD on the browser's receiver
  reports).
- ``Session``: what a viewer's session did, for the summary line.
"""

from __future__ import annotations

import ipaddress
from collections import deque
from dataclasses import dataclass, field

# --- routes -------------------------------------------------------------------

TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def classify(address: str, candidate_type: str = "", local: str = "") -> str:
    """The route a connection to ``address`` takes. With ``local`` (our end
    of the pair) a public address on our own network is the LAN too: IPv6
    has no NAT, so a phone at home talks to the machine over global
    addresses sharing its /64 (and so would hosts on a public IPv4 /24)."""
    if candidate_type == "relay":
        return "Relay"
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return "?"
    if ip.version == 4 and ip in TAILSCALE_V4:
        return "Tailscale"
    if ip.version == 6 and ip in TAILSCALE_V6:
        return "Tailscale"
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return "LAN"
    if local and _same_network(ip, local):
        return "LAN"
    return f"Internet · IPv{ip.version}"


def _same_network(ip, local: str) -> bool:
    try:
        ours = ipaddress.ip_address(local.split("%")[0])
    except ValueError:
        return False
    if ours.version != ip.version:
        return False
    prefix = 64 if ip.version == 6 else 24
    net = ipaddress.ip_network(f"{ours}/{prefix}", strict=False)
    return ip in net


def describe_candidate(candidate: str) -> str:
    """``candidate:1 1 UDP 2015363327 2001:db8::1 46569 typ host`` -> ``host
    udp ipv6``; a browser's hidden address (``….local``) -> ``host udp mdns``."""
    parts = candidate.split()
    if len(parts) < 8 or "typ" not in parts:
        return "unparsed"
    address = parts[4]
    kind = parts[parts.index("typ") + 1]
    if address.endswith(".local"):
        family = "mdns"
    else:
        try:
            family = f"ipv{ipaddress.ip_address(address.split('%')[0]).version}"
        except ValueError:
            family = "?"
    return f"{kind} {parts[2].lower()} {family}"


# --- stats ----------------------------------------------------------------------


@dataclass
class Snapshot:
    """The figures that matter from one ``get-stats`` reply (video only)."""

    local: str = ""  # selected pair, local side: "address port type"
    remote: str = ""
    local_address: str = ""
    remote_address: str = ""
    remote_type: str = ""
    protocol: str = ""
    report: int | None = None  # the browser's last receiver report (its seq)
    fraction_lost: float | None = None  # 0..1, over that report's interval
    rtt: float | None = None  # seconds
    packets_lost: int = 0
    packets_sent: int = 0
    bytes_sent: int = 0
    keyframe_requests: int = 0  # PLI + FIR

    @property
    def route(self) -> str:
        if not self.remote_address:
            return ""
        return classify(self.remote_address, self.remote_type, self.local_address)


def _by_id(stats: list[dict]) -> dict[str, dict]:
    return {s["id"]: s for s in stats if isinstance(s.get("id"), str)}


def read_snapshot(stats: list[dict]) -> Snapshot:
    """``stats``: one dict per stats structure, its name under ``_name`` (the
    streamer converts ``webrtcbin``'s reply)."""
    snap = Snapshot()
    by_id = _by_id(stats)
    for entry in stats:
        name = entry.get("_name")
        if name == "transport" and entry.get("selected-candidate-pair-id"):
            pair = by_id.get(entry["selected-candidate-pair-id"], {})
            local = by_id.get(pair.get("local-candidate-id", ""), {})
            remote = by_id.get(pair.get("remote-candidate-id", ""), {})
            if local and remote:
                snap.local = f"{local.get('address')} {local.get('port')} {local.get('candidate-type')}"
                snap.remote = f"{remote.get('address')} {remote.get('port')} {remote.get('candidate-type')}"
                snap.local_address = str(local.get("address") or "")
                snap.remote_address = str(remote.get("address") or "")
                snap.remote_type = str(remote.get("candidate-type") or "")
                snap.protocol = str(local.get("protocol") or "")
        elif name == "outbound-rtp" and entry.get("kind") == "video":
            snap.packets_sent = int(entry.get("packets-sent") or 0)
            snap.bytes_sent = int(entry.get("bytes-sent") or 0)
            snap.keyframe_requests = int(entry.get("pli-count") or 0) + int(
                entry.get("fir-count") or 0
            )
        elif name == "remote-inbound-rtp" and entry.get("kind") == "video":
            source = entry.get("gst-rtpsource-stats") or {}
            if source.get("have-rb"):
                snap.report = source.get("rb-exthighestseq")
                snap.fraction_lost = float(entry.get("fraction-lost") or 0.0)
                rtt = float(entry.get("round-trip-time") or 0.0)
                snap.rtt = rtt if rtt > 0 else None
                lost = int(entry.get("packets-lost") or 0)
                snap.packets_lost = max(lost, 0)
    return snap


# --- bitrate --------------------------------------------------------------------


RTT_BASELINE_MIN = 0.025  # seconds
KEYFRAME_ANSWER_S = 0.5  # a keyframe this soon after a request answers it
RTT_WINDOW = 30  # reports (about 30 s with Chrome's one a second)


@dataclass
class RateControl:
    """AIMD on the browser's receiver reports, one step per new report.

    Heavy loss (over 10 %) or a round trip over twice the baseline: x0.7.
    Some loss (2 to 10 %): x0.9. A clean report (under 2 %, round trip within
    1.3x the baseline) three times in a row: x1.08, and again on each clean
    report after that. Always between ``floor`` and ``ceiling``. The
    baseline is the best round trip of the last ``RTT_WINDOW`` reports (a
    path whose latency rose for good, a phone on another cell, gets a new
    baseline instead of being held at the floor), never under
    ``RTT_BASELINE_MIN``: on a LAN a sub-millisecond trip that "doubles" is
    noise, not congestion.
    """

    ceiling: int  # kbit/s: the display size's bitrate
    rate: int = 0
    floor: int = 0
    clean: int = 0
    rtts: deque = field(default_factory=lambda: deque(maxlen=RTT_WINDOW))

    def __post_init__(self) -> None:
        self.floor = self.floor or max(600, self.ceiling // 10)
        self.rate = self.rate or self.ceiling

    def update(self, loss: float, rtt: float | None) -> int:
        if rtt is not None:
            self.rtts.append(rtt)
        baseline = max(min(self.rtts, default=0.0), RTT_BASELINE_MIN)
        inflated = 1.0 if rtt is None else rtt / baseline
        if loss > 0.10 or inflated > 2.0:
            factor, self.clean = 0.7, 0
        elif loss >= 0.02:
            factor, self.clean = 0.9, 0
        elif inflated <= 1.3:
            self.clean += 1
            factor = 1.08 if self.clean >= 3 else 1.0
        else:
            factor, self.clean = 1.0, 0  # between: hold
        self.rate = max(self.floor, min(self.ceiling, round(self.rate * factor)))
        return self.rate

    @property
    def level(self) -> int:
        """The chip's gauge: 1 to 4 bars (85 %, 60 %, 35 %)."""
        share = self.rate / self.ceiling
        return 4 if share >= 0.85 else 3 if share >= 0.60 else 2 if share >= 0.35 else 1


# --- keyframes -----------------------------------------------------------------


@dataclass
class KeyframeAnswers:
    """Keyframe requests that reached the encoder, and the ones it answered
    with a keyframe within ``KEYFRAME_ANSWER_S``. Requests while one is
    waiting (and still fresh) are one request (webrtcbin coalesces bursts
    too); a request left unanswered past the window expires, so a later one
    starts its own."""

    forwarded: int = 0
    answered: int = 0
    waiting_since: float | None = None

    def asked(self, now: float) -> None:
        self.forwarded += 1
        stale = (
            self.waiting_since is not None
            and now - self.waiting_since > KEYFRAME_ANSWER_S
        )
        if self.waiting_since is None or stale:
            self.waiting_since = now

    def keyframe(self, now: float) -> None:
        if self.waiting_since is None:
            return
        if now - self.waiting_since <= KEYFRAME_ANSWER_S:
            self.answered += 1
        self.waiting_since = None


# --- the session ----------------------------------------------------------------


@dataclass
class Session:
    """What a viewer's session did, for its summary line."""

    connected_at: float | None = None
    first: Snapshot | None = None
    last: Snapshot | None = None
    rates: list[int] = field(default_factory=list)
    keyframes: KeyframeAnswers = field(default_factory=KeyframeAnswers)

    def summary(self, now: float) -> str:
        if self.connected_at is None or self.last is None or self.first is None:
            return "session: never connected"
        seconds = max(now - self.connected_at, 0.001)
        minutes, rest = divmod(int(seconds), 60)
        sent = (self.last.bytes_sent - self.first.bytes_sent) * 8 / seconds / 1000
        lost, packets = self.last.packets_lost, self.last.packets_sent
        loss = 100 * lost / packets if packets else 0.0
        rates = f"{min(self.rates)}-{max(self.rates)}" if self.rates else "?"
        return (
            f"session: {minutes}m{rest:02d}s via {self.last.route or '?'}, "
            f"sent {sent:.0f} kbit/s on average, rate {rates} kbit/s, "
            f"loss {loss:.1f} %, {self.last.keyframe_requests} keyframe requests "
            f"({self.keyframes.forwarded} to the encoder, "
            f"{self.keyframes.answered} answered)"
        )
