"""UPnP on the home router: let a viewer's stream in for its session's length.

Standard library only: the streamer (system Python) imports it next to it.
Merlin does this itself rather than leave it to libnice, which maps a port
for every candidate and never removes it.

- ``discover``: find the Internet Gateway Device (SSDP), read its services.
- ``Gateway``: IPv4 port mappings (``WANIPConnection``, IGD v1 and v2) and
  IPv6 pinholes (``WANIPv6FirewallControl``).
- ``Opener``: one stream's openings, on a worker thread: a mapping or a
  pinhole for each of the stream's host candidates (UDP), renewed before
  their lease ends, removed when the stream ends; leftovers of streamers
  that died (``Merlin <app> <pid>``, their pid gone) are removed first.
  A router that refuses a pinhole for any remote port (MiniUPnPd 1.9 on a
  Bbox answers 402) gets one per port the browser announces: its candidates
  hide its address (mDNS) but not its port, which IPv6 does not translate.

Everything the router says is data: its URLs must stay on the router's own
private address, and replies are read with a size limit.
"""

from __future__ import annotations

import ipaddress
import os
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

SSDP = ("239.255.255.250", 1900)
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
)
WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
FIREWALL6 = "urn:schemas-upnp-org:service:WANIPv6FirewallControl:1"
LEASE_S = 3600
RENEW_S = 1800
DISCOVER_S = 2.0
HTTP_TIMEOUT_S = 3.0
MAX_REPLY = 256 * 1024
UDP = 17

# UPnP error codes the openings react to.
INVALID_ARGS = 402
CONFLICT = 718  # ConflictInMappingEntry
PERMANENT_ONLY = 725  # OnlyPermanentLeasesSupported
NO_SUCH_ENTRY = 714
INVALID_INDEX = 713


class UPnPError(Exception):
    def __init__(self, code: int, text: str) -> None:
        super().__init__(f"{code} {text}".strip())
        self.code = code
        self.text = text


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _fetch(url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return response.read(MAX_REPLY)
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_REPLY) if exc.fp is not None else b""
        raise _soap_fault(body) or UPnPError(exc.code, "HTTP error") from None


def _soap_fault(body: bytes) -> UPnPError | None:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    code, text = None, ""
    for el in root.iter():
        name = _local_name(el.tag)
        if name == "errorCode" and el.text:
            try:
                code = int(el.text.strip())
            except ValueError:
                code = None
        elif name == "errorDescription" and el.text:
            text = el.text.strip()
    return UPnPError(code, text) if code is not None else None


def _router_address(url: str) -> str | None:
    """The router's address in one of its URLs: an IP literal on a private
    network over plain HTTP, or None (anything else is not followed)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        return None
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return None
    if not (ip.is_private or ip.is_link_local):
        return None
    return str(ip)


# --- the router -------------------------------------------------------------------


@dataclass
class Gateway:
    location: str
    address: str  # the router's IP
    local_ip: str  # ours, towards it
    model: str = ""
    wan: tuple[str, str] | None = None  # (service type, control URL)
    firewall6: tuple[str, str] | None = None

    def call(self, service: tuple[str, str], action: str, args=()) -> dict:
        kind, url = service
        inner = "".join(
            f"<{name}>{_escape(str(value))}</{name}>" for name, value in args
        )
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{kind}">{inner}</u:{action}></s:Body>'
            "</s:Envelope>"
        ).encode()
        reply = _fetch(
            url,
            data=body,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{kind}#{action}"',
            },
        )
        try:
            root = ET.fromstring(reply)
        except ET.ParseError:
            raise UPnPError(0, f"unreadable reply to {action}") from None
        out = {}
        for el in root.iter():
            if len(el) == 0 and el.text is not None:
                out[_local_name(el.tag)] = el.text.strip()
        return out

    # IPv4: port mappings

    def external_ip(self) -> str:
        if self.wan is None:
            return ""
        return self.call(self.wan, "GetExternalIPAddress").get(
            "NewExternalIPAddress", ""
        )

    def map_udp(
        self,
        port: int,
        description: str,
        lease: int = LEASE_S,
        external_port: int = 0,
    ) -> tuple[int, int]:
        """Map an external UDP port to ``port`` on our address: the same
        number (or ``external_port``, when renewing), else, when taken,
        another one the router picks (IGD v2). Returns ``(external_port,
        lease)``; lease 0 is permanent (a router that only takes those: removed
        at the end all the same)."""
        assert self.wan is not None
        wanted = external_port or port
        args = [
            ("NewRemoteHost", ""),
            ("NewExternalPort", wanted),
            ("NewProtocol", "UDP"),
            ("NewInternalPort", port),
            ("NewInternalClient", self.local_ip),
            ("NewEnabled", 1),
            ("NewPortMappingDescription", description),
            ("NewLeaseDuration", lease),
        ]
        try:
            self.call(self.wan, "AddPortMapping", args)
            return wanted, lease
        except UPnPError as exc:
            if exc.code == PERMANENT_ONLY and lease:
                return self.map_udp(port, description, 0, external_port)
            if exc.code != CONFLICT or external_port or not self.wan[0].endswith(":2"):
                raise
        reply = self.call(self.wan, "AddAnyPortMapping", args)
        return int(reply.get("NewReservedPort") or 0), lease

    def unmap_udp(self, external_port: int) -> None:
        assert self.wan is not None
        self.call(
            self.wan,
            "DeletePortMapping",
            [
                ("NewRemoteHost", ""),
                ("NewExternalPort", external_port),
                ("NewProtocol", "UDP"),
            ],
        )

    def mappings(self, limit: int = 1024) -> list[dict]:
        if self.wan is None:
            return []
        out = []
        for index in range(limit):
            try:
                reply = self.call(
                    self.wan,
                    "GetGenericPortMappingEntry",
                    [("NewPortMappingIndex", index)],
                )
            except UPnPError:
                break  # 713 past the end (some routers answer 500)
            out.append(reply)
        return out

    # IPv6: firewall pinholes

    def pinholes_allowed(self) -> str:
        """``open`` (no firewall), ``allowed``, ``refused``, or ``""``."""
        if self.firewall6 is None:
            return ""
        reply = self.call(self.firewall6, "GetFirewallStatus")
        if reply.get("FirewallEnabled") == "0":
            return "open"
        return "allowed" if reply.get("InboundPinholeAllowed") == "1" else "refused"

    def pinhole_udp(
        self, address: str, port: int, lease: int = LEASE_S, remote_port: int = 0
    ) -> str:
        """Let UDP from any host in to ``address``:``port``: from any port
        (``remote_port`` 0), or from that one."""
        assert self.firewall6 is not None
        reply = self.call(
            self.firewall6,
            "AddPinhole",
            [
                ("RemoteHost", ""),
                ("RemotePort", remote_port),
                ("InternalClient", address),
                ("InternalPort", port),
                ("Protocol", UDP),
                ("LeaseTime", lease),
            ],
        )
        return reply.get("UniqueID", "")

    def renew_pinhole(self, unique_id: str, lease: int = LEASE_S) -> None:
        assert self.firewall6 is not None
        self.call(
            self.firewall6,
            "UpdatePinhole",
            [("UniqueID", unique_id), ("NewLeaseTime", lease)],
        )

    def remove_pinhole(self, unique_id: str) -> None:
        assert self.firewall6 is not None
        self.call(self.firewall6, "DeletePinhole", [("UniqueID", unique_id)])


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _services(location: str, xml: bytes) -> tuple[str, list[tuple[str, str]]]:
    """The model name and every ``(serviceType, control URL)`` on the
    router's own address."""
    root = ET.fromstring(xml)
    model = ""
    base = location
    for el in root.iter():
        name = _local_name(el.tag)
        if name == "URLBase" and el.text:
            base = el.text.strip()
        elif name == "modelName" and el.text and not model:
            model = el.text.strip()
    host = _router_address(location)
    if host is None:
        return model, []
    services = []
    for el in root.iter():
        if _local_name(el.tag) != "service":
            continue
        fields = {_local_name(c.tag): (c.text or "").strip() for c in el}
        url = urllib.parse.urljoin(base, fields.get("controlURL", ""))
        if fields.get("serviceType") and _router_address(url) == host:
            services.append((fields["serviceType"], url))
    return model, services


def _search(timeout: float, target: tuple[str, int]) -> list[str]:
    """LOCATION headers of the gateways answering an SSDP search."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.25)
    found: list[str] = []
    try:
        for st in SEARCH_TARGETS:
            message = (
                "M-SEARCH * HTTP/1.1\r\n"
                f"HOST: {SSDP[0]}:{SSDP[1]}\r\n"
                'MAN: "ssdp:discover"\r\n'
                "MX: 1\r\n"
                f"ST: {st}\r\n\r\n"
            ).encode()
            sock.sendto(message, target)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, _ = sock.recvfrom(4096)
            except TimeoutError:
                if found:
                    break  # answers come together: no need to wait the rest
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                url = m.group(1).decode(errors="replace")
                if url not in found and _router_address(url):
                    found.append(url)
    finally:
        sock.close()
    return found


def discover(timeout: float = DISCOVER_S, target: tuple[str, int] = SSDP):
    """The home router, or None (no answer, no usable service)."""
    for location in _search(timeout, target):
        try:
            model, services = _services(location, _fetch(location))
        except (UPnPError, urllib.error.URLError, OSError, ET.ParseError):
            continue
        address = _router_address(location) or ""
        wan = next((s for kind in WAN_SERVICES for s in services if s[0] == kind), None)
        firewall6 = next((s for s in services if s[0] == FIREWALL6), None)
        if wan is None and firewall6 is None:
            continue
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect((address, 1900))
            local_ip = probe.getsockname()[0]
            probe.close()
        except OSError:
            continue
        return Gateway(location, address, local_ip, model, wan, firewall6)
    return None


# --- one stream's openings -----------------------------------------------------------


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


DESCRIPTION = re.compile(r"^Merlin (\S+) (\d+)$")


MAX_REMOTE_PORTS = 16  # per-port pinholes for one opening, at most


@dataclass
class Opening:
    port: int  # ours (the host candidate's)
    address: str
    external_ip: str = ""
    external_port: int = 0
    lease: int = LEASE_S
    pinhole: str = ""  # IPv6, any remote port: the router's id for it
    # IPv6 on a router that wants a remote port: remote port -> pinhole id.
    per_port: dict[int, str] | None = None

    @property
    def ipv6(self) -> bool:
        return bool(self.pinhole) or self.per_port is not None

    def pinhole_ids(self) -> list[str]:
        return ([self.pinhole] if self.pinhole else []) + list(
            (self.per_port or {}).values()
        )


@dataclass
class Opener:
    """A stream's openings on the router, handled on a worker thread.

    ``add(address, port)`` for each host candidate (any thread). Results go
    to ``on_change(status, openings)`` and, for an IPv4 mapping, to
    ``on_mapped(opening)`` (the streamer sends the browser a candidate for
    the external address). ``close()`` removes everything (waits a little)."""

    app: str
    on_change: object = None
    on_mapped: object = None
    discover: object = None
    clock: object = time.monotonic
    renew_every: float = RENEW_S
    status: str = "searching"
    openings: list[Opening] = field(default_factory=list)
    gateway: Gateway | None = None

    def __post_init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._remote_ports: list[int] = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def add(self, address: str, port: int) -> None:
        self._queue.put(("add", address, port))

    def add_remote(self, port: int) -> None:
        """A port the browser announced (its UDP candidates)."""
        self._queue.put(("remote", port))

    def reset(self) -> None:
        """Remove the openings (a rebuilt pipeline has new ports)."""
        self._queue.put(("reset",))

    def close(self, wait: float = 3.0) -> None:
        self._queue.put(("close",))
        self._thread.join(wait)

    # -- the worker ----------------------------------------------------------------

    def _run(self) -> None:
        find = self.discover or discover
        try:
            self.gateway = find()
        except Exception:  # an odd router must never stop the stream
            self.gateway = None
        if self.gateway is None:
            self._set("no router")
        else:
            self._sweep()
            self._set("router found")
        renew_at = self.clock() + self.renew_every
        while True:
            try:
                item = self._queue.get(timeout=max(1.0, renew_at - self.clock()))
            except queue.Empty:
                item = ("renew",)
            if item[0] == "close":
                self._remove_all()
                return
            if self.gateway is None:
                continue
            if item[0] == "add":
                self._open(item[1], item[2])
            elif item[0] == "remote":
                self._remote(item[1])
            elif item[0] == "reset":
                self._remove_all()
                self._set("router found")
            elif item[0] == "renew" or self.clock() >= renew_at:
                self._renew()
                renew_at = self.clock() + self.renew_every

    def _set(self, status: str) -> None:
        self.status = status
        if callable(self.on_change):
            self.on_change(status, list(self.openings))

    def _sweep(self) -> None:
        """Mappings of streamers that died (``Merlin <app> <pid>`` on our
        address, the pid gone). Another app's live stream is never touched."""
        gw = self.gateway
        try:
            entries = gw.mappings()
        except Exception:
            return
        for entry in entries:
            m = DESCRIPTION.match(entry.get("NewPortMappingDescription", ""))
            if not m or entry.get("NewInternalClient") != gw.local_ip:
                continue
            if pid_alive(int(m.group(2))):
                continue
            try:
                gw.unmap_udp(int(entry.get("NewExternalPort") or 0))
            except Exception:
                pass

    def _open(self, address: str, port: int) -> None:
        gw = self.gateway
        try:
            ip = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            return
        if any(o.port == port and o.address == address for o in self.openings):
            return
        try:
            if ip.version == 4 and gw.wan is not None and address == gw.local_ip:
                external_port, lease = gw.map_udp(
                    port, f"Merlin {self.app} {os.getpid()}"
                )
                opening = Opening(port, address, gw.external_ip(), external_port, lease)
                self.openings.append(opening)
                self._set(self._describe())
                if callable(self.on_mapped) and opening.external_ip:
                    self.on_mapped(opening)
            elif ip.version == 6 and ip.is_global and gw.firewall6 is not None:
                allowed = gw.pinholes_allowed()
                if allowed == "open":
                    return  # no firewall in the way: nothing to open
                if allowed != "allowed":
                    self._set("pinholes refused by the router")
                    return
                try:
                    unique_id = gw.pinhole_udp(address, port)
                except UPnPError as exc:
                    if exc.code != INVALID_ARGS:
                        raise
                    # It wants the remote port: one pinhole per port the
                    # browser announces (those so far, and those to come).
                    opening = Opening(port, address, per_port={})
                    self.openings.append(opening)
                    for remote in self._remote_ports:
                        self._pinhole_for(opening, remote)
                    self._set(self._describe())
                    return
                self.openings.append(Opening(port, address, pinhole=unique_id))
                self._set(self._describe())
        except UPnPError as exc:
            self._set(f"refused ({exc})")
        except (urllib.error.URLError, OSError, ValueError):
            self._set("router not answering")

    def _remote(self, port: int) -> None:
        if port in self._remote_ports or not 0 < port < 65536:
            return
        self._remote_ports.append(port)
        changed = False
        for o in self.openings:
            if o.per_port is not None:
                changed |= self._pinhole_for(o, port)
        if changed:
            self._set(self._describe())

    def _pinhole_for(self, opening: Opening, remote_port: int) -> bool:
        assert opening.per_port is not None
        if remote_port in opening.per_port or len(opening.per_port) >= MAX_REMOTE_PORTS:
            return False
        try:
            opening.per_port[remote_port] = self.gateway.pinhole_udp(
                opening.address, opening.port, remote_port=remote_port
            )
            return True
        except UPnPError as exc:
            self._set(f"refused ({exc})")
        except (urllib.error.URLError, OSError, ValueError):
            self._set("router not answering")
        return False

    def _describe(self) -> str:
        parts = []
        for o in self.openings:
            if o.pinhole:
                parts.append(f"pinhole [{o.address}]:{o.port}")
            elif o.per_port is not None:
                ports = ", ".join(str(p) for p in o.per_port) or "none yet"
                parts.append(f"pinhole [{o.address}]:{o.port} from ports {ports}")
            else:
                parts.append(f"mapped {o.external_ip}:{o.external_port}")
        return ", ".join(parts) or "router found"

    def _renew(self) -> None:
        gw = self.gateway
        for o in self.openings:
            try:
                if o.ipv6:
                    for unique_id in o.pinhole_ids():
                        gw.renew_pinhole(unique_id)
                elif o.lease:
                    gw.map_udp(
                        o.port,
                        f"Merlin {self.app} {os.getpid()}",
                        external_port=o.external_port,
                    )
            except Exception:
                pass  # the next renewal tries again; the lease covers a miss

    def _remove_all(self) -> None:
        gw = self.gateway
        for o in self.openings:
            if o.ipv6:
                for unique_id in o.pinhole_ids():
                    try:
                        gw.remove_pinhole(unique_id)
                    except Exception:
                        pass  # the lease ends it (an hour at most)
                continue
            try:
                gw.unmap_udp(o.external_port)
            except Exception:
                pass
        self.openings = []

    @property
    def ports(self) -> set[int]:
        """Our ports the router lets traffic in to."""
        return {o.port for o in self.openings if not o.ipv6 or o.pinhole_ids()}
