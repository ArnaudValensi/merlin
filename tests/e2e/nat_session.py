"""A stream across a small internet made of network namespaces (run by
test_app_reach.py inside ``unshare -rn``: root of its own user namespace,
no effect on the machine's network).

    phone 10.0.0.2 ── phonegw (NAT) 37.170.20.2 ┐
                                                inet ── coturn 51.15.30.1
    home 192.168.1.2 ── homegw (NAT) 82.66.10.2 ┘      + a fake portal

The addresses look public (the namespace has no way out, so they cannot
clash with real ones): the chip then names the route as it would outside.
Both routers masquerade and drop what nobody inside asked for (conntrack),
as home and mobile routers do; ``--phone-nat symmetric`` gives the phone a
new port per destination (``--random-fully``), so STUN alone cannot punch
through and only the relay connects. The home router forwards Merlin's TCP
port (the signaling, as merlincloud.dev does), never UDP. A throwaway Merlin
in "home" streams an app drawing a moving ball; its ICE servers come from
the fake portal (STUN and TURN credentials for the coturn, as
``/api/instance/ice`` gives them); headless Chromium in "phone" watches it,
authenticated like the portal's proxy (``X-Portal-Auth``). No UPnP router
answers here.

Prints one JSON object: the chip (and its detail), the stream state, frames
shown and the browser's inbound video counters each second, and the
streamer's ``ice:``/``upnp:``/``session:`` lines. ``NAT_DEBUG=1`` adds the
routers' counters and UDP flows while ICE checks, the pairs Chrome tried,
its ICE events, and coturn's verbose log in the session's temporary
directory: what found that a router accepting packets addressed to itself
breaks hole punching (its flow makes the masquerade change ports).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET = "e2e-relay-secret"
TURN_IP = "51.15.30.1"
HOME_PUBLIC = "82.66.10.2"
PORT = 3199
TOKEN = "mrl_" + "e2e0" * 8
SAMPLE_S = 8


class Netns:
    """A network namespace held open by a sleeping process."""

    def __init__(self, name: str) -> None:
        self.name = name
        mine = os.readlink("/proc/self/ns/net")
        self.proc = subprocess.Popen(["unshare", "--net", "sleep", "900"])
        for _ in range(200):
            try:
                if os.readlink(f"/proc/{self.proc.pid}/ns/net") != mine:
                    break
            except OSError:
                pass
            time.sleep(0.01)
        self.run("ip", "link", "set", "lo", "up")

    def prefix(self) -> list[str]:
        return ["nsenter", "-t", str(self.proc.pid), "-n", "--"]

    def run(self, *cmd: str) -> None:
        subprocess.run([*self.prefix(), *cmd], check=True)

    def popen(self, cmd: list[str], **kw) -> subprocess.Popen:
        return subprocess.Popen([*self.prefix(), *cmd], **kw)

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()


def here(*cmd: str) -> None:
    subprocess.run(cmd, check=True)


def wire(a: Netns | None, a_if: str, a_addr: str, b: Netns, b_if: str, b_addr: str):
    """A veth between two namespaces (None: this one, the internet)."""
    here("ip", "link", "add", a_if, "type", "veth", "peer", "name", b_if)
    for ns, ifname, addr in ((a, a_if, a_addr), (b, b_if, b_addr)):
        if ns is not None:
            here("ip", "link", "set", ifname, "netns", str(ns.proc.pid))
        run = ns.run if ns is not None else here
        run("ip", "addr", "add", addr, "dev", ifname)
        run("ip", "link", "set", ifname, "up")


def router(gw: Netns, wan: str, lan: str, symmetric: bool, forward: str = "") -> None:
    gw.run("sh", "-c", "echo 1 > /proc/sys/net/ipv4/ip_forward")
    masquerade = ["--random-fully"] if symmetric else []
    gw.run(
        "iptables",
        "-t",
        "nat",
        "-A",
        "POSTROUTING",
        "-o",
        wan,
        "-j",
        "MASQUERADE",
        *masquerade,
    )
    # Nothing from outside reaches the router itself unless asked for: an
    # accepted packet would leave a flow behind and make the masquerade pick
    # another port for the inside host's own packet (punching then fails).
    gw.run(
        "iptables",
        "-A",
        "INPUT",
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED,RELATED",
        "-j",
        "ACCEPT",
    )
    gw.run("iptables", "-A", "INPUT", "-i", wan, "-j", "DROP")
    gw.run("iptables", "-P", "FORWARD", "DROP")
    gw.run(
        "iptables",
        "-A",
        "FORWARD",
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED,RELATED",
        "-j",
        "ACCEPT",
    )
    gw.run("iptables", "-A", "FORWARD", "-i", lan, "-j", "ACCEPT")
    if forward:  # Merlin's TCP port only: the signaling's way in
        gw.run(
            "iptables",
            "-t",
            "nat",
            "-A",
            "PREROUTING",
            "-i",
            wan,
            "-p",
            "tcp",
            "--dport",
            str(PORT),
            "-j",
            "DNAT",
            "--to-destination",
            f"{forward}:{PORT}",
        )
        gw.run(
            "iptables",
            "-A",
            "FORWARD",
            "-i",
            wan,
            "-p",
            "tcp",
            "-d",
            forward,
            "--dport",
            str(PORT),
            "-j",
            "ACCEPT",
        )


def credentials() -> dict:
    username = f"{int(time.time()) + 3600}:e2e"
    digest = hmac.new(SECRET.encode(), username.encode(), hashlib.sha1).digest()
    return {
        "urls": [f"turn:{TURN_IP}:3478?transport=udp"],
        "username": username,
        "credential": base64.b64encode(digest).decode(),
    }


class Portal(BaseHTTPRequestHandler):
    """/api/instance/ice as the real portal answers it (``relay`` False: an
    account without access, STUN only)."""

    relay = True

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        if (
            self.path != "/api/instance/ice"
            or self.headers.get("Authorization") != f"Bearer {TOKEN}"
        ):
            self.send_error(404)
            return
        servers = [{"urls": [f"stun:{TURN_IP}:3478"]}]
        if self.relay:
            servers.append(credentials())
        body = json.dumps(
            {
                "iceServers": servers,
                "ttl": 3600,
                "turn": "ok" if self.relay else "no access",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


PAIRS = """async () => {
    const out = [];
    for (const pc of window.__pcs || []) {
        const byId = {}, pairs = [];
        (await pc.getStats()).forEach((s) => {
            byId[s.id] = s;
            if (s.type === 'candidate-pair') pairs.push(s);
        });
        const c = (id) => {
            const x = byId[id] || {};
            return x.candidateType + ' ' + (x.address || x.ip) + ':' + x.port;
        };
        out.push(pairs.map((p) => p.state + ' ' + c(p.localCandidateId) + ' -> ' +
            c(p.remoteCandidateId) + ' sent ' + p.requestsSent + ' got ' + p.responsesReceived));
    }
    return out;
}"""

LIVE = "document.getElementById('player').dataset.streamState === 'live'"

INBOUND = """async () => {
    const pc = (window.__pcs || []).slice(-1)[0];
    if (!pc) return null;
    let v = null;
    (await pc.getStats()).forEach((s) => {
        if (s.type === 'inbound-rtp' && s.kind === 'video') v = s;
    });
    return v && [v.packetsReceived, v.packetsLost, v.framesDecoded, v.keyFramesDecoded,
                 v.pliCount, v.nackCount, v.framesDropped, v.bytesReceived];
}"""


def driver(url: str) -> int:
    """In the phone: watch the stream, report what the chip and video did."""
    from playwright.sync_api import sync_playwright

    debug = bool(os.environ.get("NAT_DEBUG"))
    out: dict = {"samples": []}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 720},
            extra_http_headers={"X-Portal-Auth": TOKEN},
        )
        page = ctx.new_page()
        if True:  # keep every peer: its pairs and its inbound stats
            page.add_init_script("""
                const Orig = window.RTCPeerConnection;
                window.__pcs = [];
                window.__events = [];
                const t0 = performance.now();
                const note = (what) => window.__events.push(
                    Math.round(performance.now() - t0) + ' ' + what);
                window.RTCPeerConnection = function (...args) {
                    const pc = new Orig(...args); window.__pcs.push(pc);
                    note('peer ' + JSON.stringify(args[0]));
                    pc.addEventListener('connectionstatechange', () => note('state ' + pc.connectionState));
                    pc.addEventListener('icegatheringstatechange', () => note('gathering ' + pc.iceGatheringState));
                    pc.addEventListener('icecandidate', (e) => note('local ' + (e.candidate ? e.candidate.candidate : 'end')));
                    return pc;
                };
                window.RTCPeerConnection.prototype = Orig.prototype;
                const add = Orig.prototype.addIceCandidate;
                Orig.prototype.addIceCandidate = function (c) {
                    note('remote ' + (c && c.candidate));
                    return add.apply(this, arguments).catch((e) => { note('remote refused ' + e); });
                };
            """)
        page.goto(url)
        deadline = time.monotonic() + 60
        while not page.evaluate(LIVE):
            if debug:
                pairs = page.evaluate(PAIRS)
                if any(pairs):
                    out["pairs"] = pairs  # the last ones seen before any teardown
                    out.setdefault("pair_history", []).append(pairs)
            if time.monotonic() > deadline:
                out["error"] = "never live"
                break
            time.sleep(1)
        for _ in range(SAMPLE_S):
            time.sleep(1)
            out.setdefault("inbound", []).append(page.evaluate(INBOUND))
            out["samples"].append(
                page.evaluate(
                    """() => {
                    const q = document.getElementById('player-video').getVideoPlaybackQuality();
                    return [document.getElementById('player').dataset.streamState,
                            q.totalVideoFrames, q.droppedVideoFrames, performance.now() / 1000];
                }"""
                )
            )
        if debug and page.evaluate(LIVE):
            out["pairs"] = page.evaluate(PAIRS)
        if debug:
            out["events"] = page.evaluate("window.__events || []")
        out["chip"] = page.evaluate(
            "document.getElementById('player-chip').textContent"
        )
        page.click("#player-chip")
        time.sleep(1.5)
        out["detail"] = page.evaluate(
            "document.getElementById('player-chip').textContent"
        )
        browser.close()
    print(json.dumps(out))
    return 0


def main(phone_nat: str, query: str, relay: bool = True) -> int:
    Portal.relay = relay
    namespaces: list[Netns] = []
    procs: list[subprocess.Popen] = []
    tmp = Path(tempfile.mkdtemp(prefix="nat-e2e-"))
    out: dict = {"phone_nat": phone_nat, "query": query}
    try:
        here("ip", "link", "set", "lo", "up")
        here("sh", "-c", "echo 1 > /proc/sys/net/ipv4/ip_forward")
        here("ip", "link", "add", "turn0", "type", "dummy")
        here("ip", "addr", "add", f"{TURN_IP}/32", "dev", "turn0")
        here("ip", "link", "set", "turn0", "up")
        # The rest of the world: a sink, as an internet would be (without a
        # route at all a send fails, which a real relay never sees).
        here("ip", "route", "add", "blackhole", "default")
        homegw, home, phonegw, phone = (
            Netns(n) for n in ("homegw", "home", "phonegw", "phone")
        )
        namespaces += [homegw, home, phonegw, phone]
        wire(None, "inet-hg", "82.66.10.1/24", homegw, "hg-wan", f"{HOME_PUBLIC}/24")
        wire(homegw, "hg-lan", "192.168.1.1/24", home, "h-eth", "192.168.1.2/24")
        wire(None, "inet-pg", "37.170.20.1/24", phonegw, "pg-wan", "37.170.20.2/24")
        wire(phonegw, "pg-lan", "10.0.0.1/24", phone, "p-eth", "10.0.0.2/24")
        homegw.run("ip", "route", "add", "default", "via", "82.66.10.1")
        phonegw.run("ip", "route", "add", "default", "via", "37.170.20.1")
        home.run("ip", "route", "add", "default", "via", "192.168.1.1")
        phone.run("ip", "route", "add", "default", "via", "10.0.0.1")
        router(homegw, "hg-wan", "hg-lan", symmetric=False, forward="192.168.1.2")
        router(phonegw, "pg-wan", "pg-lan", symmetric=phone_nat == "symmetric")

        procs.append(
            subprocess.Popen(
                [
                    "turnserver",
                    "-n",
                    "--listening-ip",
                    TURN_IP,
                    "--relay-ip",
                    TURN_IP,
                    "--listening-port",
                    "3478",
                    "--min-port",
                    "49152",
                    "--max-port",
                    "49400",
                    "--use-auth-secret",
                    "--static-auth-secret",
                    SECRET,
                    "--realm",
                    "e2e",
                    # As in production (infra/turnserver.conf): private peers refused.
                    "--denied-peer-ip",
                    "10.0.0.0-10.255.255.255",
                    "--denied-peer-ip",
                    "172.16.0.0-172.31.255.255",
                    "--denied-peer-ip",
                    "192.168.0.0-192.168.255.255",
                    # and itself: two relays of the same server never reach each other
                    "--denied-peer-ip",
                    TURN_IP,
                    "--no-tls",
                    "--log-file",
                    str(tmp / "turn.log"),
                    "--simple-log",
                    *(["--verbose"] if os.environ.get("NAT_DEBUG") else []),
                    "--pidfile",
                    str(tmp / "turn.pid"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        portal = ThreadingHTTPServer((TURN_IP, 8099), Portal)
        threading.Thread(target=portal.serve_forever, daemon=True).start()

        mhome = tmp / "merlin-home"
        mhome.mkdir()
        (mhome / "config.env").write_text("")
        user = tmp / "user"
        user.mkdir()
        (user / ".zshrc").write_text("")
        drop = (
            "MERLIN_SAAS_TOKEN",
            "MERLIN_ENVIRONMENT_SLUG",
            "TMUX",
            "VIRTUAL_ENV",
            "MERLIN_APP_STUN",
            "MERLIN_APP_UPNP",
        )
        env = {k: v for k, v in os.environ.items() if k not in drop}
        env.update(
            MERLIN_HOME=str(mhome),
            HOME=str(user),
            MERLIN_DEV="1",
            MERLIN_FEATURES="app",
            DASHBOARD_PASS="",
            DISCORD_BOT_TOKEN="",
            UV_OFFLINE="1",
            TMUX_TMPDIR=tempfile.mkdtemp(prefix="nt-"),
            # Merlin Cloud, as far as this Merlin can tell: the fake portal.
            MERLIN_SAAS_TOKEN=TOKEN,
            MERLIN_SAAS_API=f"http://{TURN_IP}:8099",
            MERLIN_APP_STUN=f"stun:{TURN_IP}:3478",
        )
        server = home.popen(
            ["uv", "run", "main.py", "--port", str(PORT)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(server)
        for _ in range(240):
            try:  # through the home router's port forward, like the phone
                conn = http.client.HTTPConnection(HOME_PUBLIC, PORT, timeout=2)
                conn.request("GET", "/apps", headers={"X-Portal-Auth": TOKEN})
                conn.getresponse().read()
                break
            except OSError:
                time.sleep(0.5)
        launched = subprocess.run(
            [
                *home.prefix(),
                str(ROOT / "app" / "commands" / "run.py"),
                "--name",
                "ball",
                "--gpu",
                "off",
                "--audio",
                "local",
                "--",
                "gst-launch-1.0",
                "-q",
                "videotestsrc",
                "pattern=ball",
                "!",
                "video/x-raw,width=1280,height=720",
                "!",
                "ximagesink",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        out["launch"] = json.loads(launched.stdout or "{}").get("status")
        snapshots: list = []
        if os.environ.get("NAT_DEBUG"):

            def snap() -> None:  # the routers' UDP flows while ICE checks
                for delay in (6, 6):
                    time.sleep(delay)
                    shot = {}
                    for name, gw in (("homegw", homegw), ("phonegw", phonegw)):
                        tracked = subprocess.run(
                            [*gw.prefix(), "cat", "/proc/net/nf_conntrack"],
                            capture_output=True,
                            text=True,
                            check=False,
                        ).stdout
                        shot[name] = [l for l in tracked.splitlines() if " udp " in l]
                    snapshots.append(shot)

            threading.Thread(target=snap, daemon=True).start()
        watched = subprocess.run(
            [
                *phone.prefix(),
                sys.executable,
                __file__,
                "--driver",
                f"http://{HOME_PUBLIC}:{PORT}/apps/ball/play{query}",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        out["conntrack"] = snapshots
        lines = watched.stdout.strip().splitlines()
        out.update(
            json.loads(lines[-1]) if lines else {"driver_error": watched.stderr[-2000:]}
        )
        time.sleep(3)  # the streamer logs its summary as the viewer leaves
        if os.environ.get("NAT_DEBUG"):
            for name, gw in (("homegw", homegw), ("phonegw", phonegw)):
                for table in ("filter", "nat"):
                    counters = subprocess.run(
                        [*gw.prefix(), "iptables", "-t", table, "-L", "-v", "-n"],
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout
                    out[f"{name}-{table}"] = counters.splitlines()
                tracked = subprocess.run(
                    [*gw.prefix(), "cat", "/proc/net/nf_conntrack"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout
                out[f"{name}-conntrack"] = [
                    line for line in tracked.splitlines() if " udp " in line
                ]
        subprocess.run(
            [*home.prefix(), str(ROOT / "app" / "commands" / "stop.py"), "ball"],
            env=env,
            capture_output=True,
            check=False,
        )
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for ns in namespaces:
            ns.close()
    log = tmp / "merlin-home" / "logs" / "merlin.log"
    lines = log.read_text().splitlines() if log.exists() else []
    out["log"] = [
        line.split("streamer: ", 1)[1]
        for line in lines
        if "streamer: " in line
        and any(
            k in line
            for k in (
                "ice: servers",
                "ice: path",
                "ice: route",
                "ice: local",
                "upnp: ",
                "session: ",
            )
            + (("ice: ",) if os.environ.get("NAT_DEBUG") else ())
        )
    ]
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--phone-nat", choices=("cone", "symmetric"), default="cone")
    parser.add_argument("--query", default="")
    parser.add_argument("--driver", default="")
    parser.add_argument(
        "--no-relay", action="store_true", help="the portal offers STUN only"
    )
    args = parser.parse_args()
    sys.exit(
        driver(args.driver)
        if args.driver
        else main(args.phone_nat, args.query, relay=not args.no_relay)
    )
