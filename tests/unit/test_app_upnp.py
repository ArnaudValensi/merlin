"""UPnP on the home router (app/upnp.py) against a fake Internet Gateway
Device: SSDP discovery, IPv4 mappings, IPv6 pinholes, and one stream's
openings from start to end."""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import upnp

WAN2 = "urn:schemas-upnp-org:service:WANIPConnection:2"
WAN1 = "urn:schemas-upnp-org:service:WANIPConnection:1"
FW6 = upnp.FIREWALL6


class FakeRouter:
    """An IGD on 127.0.0.1: its description, its SOAP actions, its state."""

    def __init__(
        self,
        wan=WAN2,
        firewall6=True,
        pinholes_allowed=True,
        firewall_on=True,
        conflicts=(),
        permanent_only=False,
        control_host="127.0.0.1",
        any_port_refused=False,
        redirect_to="",
        external_ip_fails=False,
        delete_delay=0.0,
    ):
        self.wan = wan
        self.firewall6 = firewall6
        self.pinholes_allowed = pinholes_allowed
        self.firewall_on = firewall_on
        self.conflicts = set(conflicts)
        self.permanent_only = permanent_only
        self.control_host = control_host
        self.any_port_refused = any_port_refused  # MiniUPnPd 1.9 on a Bbox
        self.redirect_to = redirect_to  # the description answers a redirect
        self.external_ip_fails = external_ip_fails
        self.delete_delay = delete_delay  # a slow router, per deletion
        self.calls: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.mappings: dict[int, dict] = {}
        self.pinholes: dict[str, dict] = {}
        self._next_pinhole = 1
        router = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                router.gets.append(self.path)
                if self.path != "/rootDesc.xml":
                    self.send_error(404)
                    return
                if router.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", router.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._reply(200, router.description().encode())

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                action = self.headers["SOAPAction"].strip('"').split("#")[1]
                args = dict(re.findall(r"<(\w+)>([^<]*)</\1>", body))
                router.calls.append((action, args))
                status, reply = router.act(action, args)
                self._reply(status, reply.encode())

            def _reply(self, status, data):
                self.send_response(status)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.ssdp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ssdp.bind(("127.0.0.1", 0))
        self.ssdp_target = self.ssdp.getsockname()
        self.location = f"http://127.0.0.1:{self.port}/rootDesc.xml"
        threading.Thread(target=self._answer_ssdp, daemon=True).start()

    def _answer_ssdp(self):
        while True:
            try:
                data, peer = self.ssdp.recvfrom(4096)
            except OSError:
                return
            if b"M-SEARCH" in data:
                self.ssdp.sendto(
                    (
                        "HTTP/1.1 200 OK\r\n"
                        f"LOCATION: {self.location}\r\n"
                        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:2\r\n\r\n"
                    ).encode(),
                    peer,
                )

    def close(self):
        self.http.shutdown()
        self.ssdp.close()

    def description(self) -> str:
        host = f"http://{self.control_host}:{self.port}"
        services = (
            f"<service><serviceType>{self.wan}</serviceType>"
            f"<controlURL>{host}/ctl/IPConn</controlURL></service>"
        )
        if self.firewall6:
            services += (
                f"<service><serviceType>{FW6}</serviceType>"
                "<controlURL>/ctl/IP6FCtl</controlURL></service>"
            )
        return (
            '<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0">'
            "<device><modelName>Fake F@st</modelName><deviceList><device>"
            f"<serviceList>{services}</serviceList>"
            "</device></deviceList></device></root>"
        )

    @staticmethod
    def ok(action, fields=""):
        return 200, (
            '<?xml version="1.0"?><s:Envelope '
            'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action}Response xmlns:u="x">{fields}</u:{action}Response>'
            "</s:Body></s:Envelope>"
        )

    @staticmethod
    def fault(code, text):
        return 500, (
            '<?xml version="1.0"?><s:Envelope '
            'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
            '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
            f"<errorCode>{code}</errorCode><errorDescription>{text}</errorDescription>"
            "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
        )

    def act(self, action, a):
        if action == "GetExternalIPAddress":
            if self.external_ip_fails:
                return self.fault(501, "ActionFailed")
            return self.ok(
                action, "<NewExternalIPAddress>176.186.26.141</NewExternalIPAddress>"
            )
        if action in ("AddPortMapping", "AddAnyPortMapping"):
            port = int(a["NewExternalPort"])
            if self.permanent_only and a["NewLeaseDuration"] != "0":
                return self.fault(725, "OnlyPermanentLeasesSupported")
            if action == "AddPortMapping" and port in self.conflicts:
                return self.fault(718, "ConflictInMappingEntry")
            if action == "AddAnyPortMapping":
                port = 61000
            self.mappings[port] = dict(a, NewExternalPort=str(port))
            if action == "AddAnyPortMapping":
                return self.ok(action, f"<NewReservedPort>{port}</NewReservedPort>")
            return self.ok(action)
        if action == "DeletePortMapping":
            time.sleep(self.delete_delay)
            if self.mappings.pop(int(a["NewExternalPort"]), None) is None:
                return self.fault(714, "NoSuchEntryInArray")
            return self.ok(action)
        if action == "GetSpecificPortMappingEntry":
            entry = self.mappings.get(int(a["NewExternalPort"]))
            if entry is None or entry.get("NewProtocol") != a["NewProtocol"]:
                return self.fault(714, "NoSuchEntryInArray")
            keep = (
                "NewInternalPort",
                "NewInternalClient",
                "NewEnabled",
                "NewPortMappingDescription",
                "NewLeaseDuration",
            )
            fields = "".join(f"<{k}>{v}</{k}>" for k, v in entry.items() if k in keep)
            return self.ok(action, fields)
        if action == "GetGenericPortMappingEntry":
            entries = sorted(
                self.mappings.values(), key=lambda e: int(e["NewExternalPort"])
            )
            index = int(a["NewPortMappingIndex"])
            if index >= len(entries):
                return self.fault(713, "SpecifiedArrayIndexInvalid")
            return self.ok(
                action, "".join(f"<{k}>{v}</{k}>" for k, v in entries[index].items())
            )
        if action == "GetFirewallStatus":
            return self.ok(
                action,
                f"<FirewallEnabled>{int(self.firewall_on)}</FirewallEnabled>"
                f"<InboundPinholeAllowed>{int(self.pinholes_allowed)}</InboundPinholeAllowed>",
            )
        if action == "AddPinhole":
            if self.any_port_refused and a.get("RemotePort") == "0":
                return self.fault(402, "Invalid Args")
            uid = str(self._next_pinhole)
            self._next_pinhole += 1
            self.pinholes[uid] = a
            return self.ok(action, f"<UniqueID>{uid}</UniqueID>")
        if action == "UpdatePinhole":
            return (
                self.ok(action)
                if a["UniqueID"] in self.pinholes
                else self.fault(704, "NoSuchEntry")
            )
        if action == "DeletePinhole":
            time.sleep(self.delete_delay)
            return (
                self.ok(action)
                if self.pinholes.pop(a["UniqueID"], None)
                else self.fault(704, "NoSuchEntry")
            )
        return self.fault(401, "InvalidAction")

    def actions(self):
        return [name for name, _ in self.calls]


@pytest.fixture
def router():
    made = []

    def make(**kw):
        r = FakeRouter(**kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def _gateway(r):
    gw = upnp.discover(timeout=1.0, target=r.ssdp_target, expect="127.0.0.1")
    assert gw is not None
    return gw


def test_the_router_is_found_and_its_services_read(router):
    r = router()
    gw = _gateway(r)
    assert gw.model == "Fake F@st"
    assert gw.address == "127.0.0.1" and gw.local_ip == "127.0.0.1"
    assert gw.wan == (WAN2, f"http://127.0.0.1:{r.port}/ctl/IPConn")
    assert gw.firewall6 == (FW6, f"http://127.0.0.1:{r.port}/ctl/IP6FCtl")
    assert gw.external_ip() == "176.186.26.141"


def test_a_router_pointing_elsewhere_is_not_followed(router):
    r = router(control_host="8.8.8.8")  # the control URL leaves the router
    gw = upnp.discover(timeout=1.0, target=r.ssdp_target, expect="127.0.0.1")
    assert gw is not None and gw.wan is None  # only the relative one is kept
    assert upnp._router_address("http://8.8.8.8:5000/desc.xml") is None
    assert upnp._router_address("https://192.168.1.254/desc.xml") is None
    assert upnp._router_address("http://router.lan/desc.xml") is None
    assert (
        upnp._router_address("http://192.168.1.254:49152/desc.xml") == "192.168.1.254"
    )


def test_nobody_answering_is_no_router():
    quiet = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    quiet.bind(("127.0.0.1", 0))
    try:
        started = time.monotonic()
        found = upnp.discover(
            timeout=0.5, target=quiet.getsockname(), expect="127.0.0.1"
        )
        assert found is None
        assert time.monotonic() - started < 2
    finally:
        quiet.close()


def test_a_taken_port_gets_another_one_on_igd2_and_fails_on_igd1(router):
    gw = _gateway(router(conflicts={40001}))
    assert gw.map_udp(40001, "Merlin a 1") == (61000, 3600)
    assert gw.map_udp(40002, "Merlin a 1") == (40002, 3600)
    gw1 = _gateway(router(wan=WAN1, conflicts={40001}))
    with pytest.raises(upnp.UPnPError) as err:
        gw1.map_udp(40001, "Merlin a 1")
    assert err.value.code == upnp.CONFLICT


def test_a_router_taking_only_permanent_mappings_gets_none(router):
    """A permanent mapping would stay open for good after a crash."""
    r = router(permanent_only=True)
    gw = _gateway(r)
    with pytest.raises(upnp.UPnPError) as err:
        gw.map_udp(40001, "Merlin a 1")
    assert err.value.code == 725
    assert r.mappings == {}
    leases = [a["NewLeaseDuration"] for n, a in r.calls if n == "AddPortMapping"]
    assert leases == ["3600"]


class Recorder:
    def __init__(self):
        self.changes, self.mapped = [], []
        self.event = threading.Event()

    def change(self, status, openings):
        self.changes.append(status)
        self.event.set()

    def wait_for(self, predicate, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False


def _opener(r, rec, **kw):
    return upnp.Opener(
        "oob",
        on_change=rec.change,
        on_mapped=rec.mapped.append,
        discover=lambda: upnp.discover(
            timeout=1.0, target=r.ssdp_target, expect="127.0.0.1"
        ),
        **kw,
    )


def test_a_stream_opens_its_ports_renews_them_and_closes_them(router):
    r = router()
    rec = Recorder()
    opener = _opener(r, rec, renew_every=0.3)
    opener.add("127.0.0.1", 40001)  # our IPv4 address towards the router
    opener.add("2a01:cb00::5", 40002)  # a global IPv6 address
    opener.add("fe80::1", 40003)  # link-local: nothing to open
    opener.add("10.9.9.9", 40004)  # another interface's address: not mapped
    assert rec.wait_for(lambda: len(opener.openings) == 2)
    assert r.mappings[40001]["NewPortMappingDescription"] == f"Merlin oob {os.getpid()}"
    assert r.mappings[40001]["NewInternalClient"] == "127.0.0.1"
    assert list(r.pinholes.values())[0]["InternalClient"] == "2a01:cb00::5"
    assert list(r.pinholes.values())[0]["Protocol"] == "17"
    assert [(o.external_ip, o.external_port) for o in rec.mapped] == [
        ("176.186.26.141", 40001)
    ]
    assert opener.status == "mapped 176.186.26.141:40001, pinhole [2a01:cb00::5]:40002"
    assert opener.ports == {40001, 40002}
    assert rec.wait_for(lambda: "UpdatePinhole" in r.actions())  # renewed
    assert rec.wait_for(lambda: r.actions().count("AddPortMapping") >= 2)
    renewals = [a for n, a in r.calls if n == "AddPortMapping"]
    assert all(a["NewExternalPort"] == "40001" for a in renewals)
    opener.close()
    assert r.mappings == {} and r.pinholes == {}


def test_dead_streamers_leftovers_go_and_nothing_else(router):
    r = router()
    dead = 999_999_999
    alive = os.getpid()
    for port, desc, client in (
        (50001, f"Merlin old {dead}", "127.0.0.1"),
        (50002, f"Merlin other {alive}", "127.0.0.1"),
        (50003, "libnice", "127.0.0.1"),
        (50004, f"Merlin old {dead}", "192.168.1.30"),  # another machine's
    ):
        r.mappings[port] = {
            "NewExternalPort": str(port),
            "NewProtocol": "UDP",
            "NewInternalPort": str(port),
            "NewInternalClient": client,
            "NewPortMappingDescription": desc,
        }
    rec = Recorder()
    opener = _opener(r, rec)
    assert rec.wait_for(lambda: opener.status == "router found")
    assert sorted(r.mappings) == [50002, 50003, 50004]
    opener.close()


def test_pinholes_follow_the_routers_firewall(router):
    refusing = router(pinholes_allowed=False)
    rec = Recorder()
    opener = _opener(refusing, rec)
    opener.add("2a01:cb00::5", 40002)
    assert rec.wait_for(lambda: opener.status == "pinholes refused by the router")
    opener.close()
    open_router = router(firewall_on=False)
    rec = Recorder()
    opener = _opener(open_router, rec)
    opener.add("2a01:cb00::5", 40002)
    opener.add("127.0.0.1", 40001)
    assert rec.wait_for(lambda: opener.status.startswith("mapped"))
    assert open_router.pinholes == {}  # no firewall: nothing to open
    opener.close()


def test_no_router_means_no_openings_and_a_quiet_close():
    rec = Recorder()
    opener = upnp.Opener("oob", on_change=rec.change, discover=lambda: None)
    opener.add("127.0.0.1", 40001)
    assert rec.wait_for(lambda: opener.status == "no router")
    opener.close()
    assert opener.openings == []


def test_a_refusing_router_never_stops_the_stream(router):
    r = router(wan=WAN1, conflicts={40001})
    rec = Recorder()
    opener = _opener(r, rec)
    opener.add("127.0.0.1", 40001)
    assert rec.wait_for(lambda: opener.status.startswith("refused (718"))
    opener.add("127.0.0.1", 40005)  # the next one still goes through
    assert rec.wait_for(lambda: 40005 in r.mappings)
    opener.close()


def test_a_rebuilt_pipeline_drops_the_old_openings(router):
    r = router()
    rec = Recorder()
    opener = _opener(r, rec)
    opener.add("127.0.0.1", 40001)
    assert rec.wait_for(lambda: 40001 in r.mappings)
    opener.reset()
    opener.add("127.0.0.1", 40009)
    assert rec.wait_for(lambda: sorted(r.mappings) == [40009])
    opener.close()
    assert r.mappings == {}


def test_a_router_wanting_remote_ports_gets_one_pinhole_per_browser_port(router):
    """The user's Bbox: no pinhole for any remote port, but one per port is
    fine; the browser's candidates give their ports even behind mDNS."""
    r = router(any_port_refused=True)
    rec = Recorder()
    opener = _opener(r, rec, renew_every=0.3)
    opener.add_remote(54321)  # announced before our own candidate
    opener.add("2a01:cb00::5", 40002)
    assert rec.wait_for(lambda: len(r.pinholes) == 1)
    opener.add_remote(54400)
    opener.add_remote(54321)  # again: nothing new
    opener.add_remote(0)  # nonsense: ignored
    assert rec.wait_for(lambda: len(r.pinholes) == 2)
    assert sorted(p["RemotePort"] for p in r.pinholes.values()) == ["54321", "54400"]
    assert {p["RemoteHost"] for p in r.pinholes.values()} == {""}  # any host
    assert opener.status == "pinhole [2a01:cb00::5]:40002 from ports 54321, 54400"
    assert opener.ports == {40002}
    assert rec.wait_for(lambda: r.actions().count("UpdatePinhole") >= 2)  # each renewed
    opener.close()
    assert r.pinholes == {}


def test_an_ipv6_opening_without_a_browser_port_lets_nothing_in_yet(router):
    r = router(any_port_refused=True)
    rec = Recorder()
    opener = _opener(r, rec)
    opener.add("2a01:cb00::5", 40002)
    assert rec.wait_for(lambda: "from ports none yet" in opener.status)
    assert opener.ports == set()  # not counted as open
    opener.close()


# --- what a device on the LAN can make Merlin do --------------------------------


def test_only_the_default_gateway_is_the_router(router):
    r = router()
    # Answering from 127.0.0.1 while the gateway is elsewhere: ignored.
    found = upnp.discover(timeout=0.5, target=r.ssdp_target, expect="192.168.1.254")
    assert found is None
    assert r.gets == []  # not even its description was fetched
    table = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "enp9s0\t00000000\tFE01A8C0\t0003\t0\t0\t100\t00000000\n"
        "enp9s0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\n"
    )
    assert upnp.default_gateway(table) == "192.168.1.254"
    assert upnp.default_gateway("Iface\tDestination\tGateway\n") is None


def test_a_redirect_is_never_followed(router):
    elsewhere = router()  # stands for anything else reachable from here
    r = router(redirect_to=f"http://127.0.0.1:{elsewhere.port}/rootDesc.xml")
    found = upnp.discover(timeout=1.0, target=r.ssdp_target, expect="127.0.0.1")
    assert found is None
    assert r.gets == ["/rootDesc.xml"]
    assert elsewhere.gets == []  # the redirect went nowhere


def test_a_failed_address_lookup_leaves_the_mapping_owned(router):
    r = router(external_ip_fails=True)
    rec = Recorder()
    opener = _opener(r, rec)
    opener.add("127.0.0.1", 40001)
    assert rec.wait_for(lambda: opener.status == "mapped ?:40001")
    assert rec.mapped == []  # no address to give the browser
    assert opener.close()
    assert r.mappings == {}  # removed all the same


def test_only_a_mapping_still_ours_is_deleted(router):
    r = router()
    rec = Recorder()
    opener = _opener(r, rec)
    opener.add("127.0.0.1", 40001)
    assert rec.wait_for(lambda: 40001 in r.mappings)
    # Our lease lapsed and another application took the port meanwhile.
    r.mappings[40001] = dict(
        r.mappings[40001],
        NewInternalClient="192.168.1.30",
        NewPortMappingDescription="game console",
    )
    assert opener.close()
    assert r.mappings[40001]["NewPortMappingDescription"] == "game console"
    assert "DeletePortMapping" not in r.actions()


def test_the_sweep_leaves_mappings_merlin_does_not_make(router):
    r = router()
    for port, protocol, remote in ((50001, "TCP", ""), (50002, "UDP", "8.8.8.8")):
        r.mappings[port] = {
            "NewExternalPort": str(port),
            "NewProtocol": protocol,
            "NewRemoteHost": remote,
            "NewInternalClient": "127.0.0.1",
            "NewPortMappingDescription": "Merlin old 999999999",
        }
    rec = Recorder()
    opener = _opener(r, rec)
    assert rec.wait_for(lambda: opener.status == "router found")
    assert sorted(r.mappings) == [50001, 50002]
    opener.close()


def test_a_slow_router_still_gets_everything_removed_in_time(router):
    r = router(delete_delay=1.6)
    rec = Recorder()
    opener = _opener(r, rec)
    opener.add("127.0.0.1", 40001)
    opener.add("2a01:cb00::5", 40002)
    assert rec.wait_for(lambda: len(opener.openings) == 2)
    started = time.monotonic()
    assert opener.close()  # both removals at once, not one after the other
    assert time.monotonic() - started < upnp.CLOSE_S
    assert r.mappings == {} and r.pinholes == {}


def test_closing_stops_what_is_queued(router):
    r = router()
    rec = Recorder()
    opener = _opener(r, rec)
    for port in range(40001, 40021):
        opener.add("127.0.0.1", port)
    assert opener.close()
    assert r.mappings == {}  # opened then removed, or never opened


def test_the_sweep_rechecks_each_mapping_before_deleting_it(router, monkeypatch):
    """The table is listed first: a leftover that lapses meanwhile and goes
    to another device must survive the sweep."""
    r = router()
    for port in (50001, 50002):
        r.mappings[port] = {
            "NewExternalPort": str(port),
            "NewProtocol": "UDP",
            "NewRemoteHost": "",
            "NewInternalPort": str(port),
            "NewInternalClient": "127.0.0.1",
            "NewPortMappingDescription": "Merlin old 999999999",
        }
    listed = upnp.Gateway.mappings

    def mappings_then_reassign(self, limit=1024):
        table = listed(self, limit)
        r.mappings[50001] = dict(
            r.mappings[50001],
            NewInternalClient="192.168.1.30",
            NewPortMappingDescription="game console",
        )
        return table

    monkeypatch.setattr(upnp.Gateway, "mappings", mappings_then_reassign)
    rec = Recorder()
    opener = _opener(r, rec)
    assert rec.wait_for(lambda: opener.status == "router found")
    assert sorted(r.mappings) == [50001]  # the other device's, kept
    assert r.mappings[50001]["NewPortMappingDescription"] == "game console"
    opener.close()
