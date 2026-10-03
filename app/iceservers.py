"""The ICE servers a stream uses to reach the machine from another network.

Standard library only: the server imports it (to fetch and cache), the
streamer (system Python) imports it next to it (to hand the servers to
webrtcbin).

- STUN for every Merlin: ``MERLIN_APP_STUN`` (``stun:host:port``; unset means
  ours, empty means none). Each end learns its public address from it, so
  both can send connectivity checks and each one's router lets the other's
  replies in.
- TURN (a relay) for an instance connected to Merlin Cloud whose account has
  access: short-lived credentials from the portal (``GET /api/instance/ice``),
  cached here until an hour before they expire. A portal that does not answer
  keeps the last good answer while it is valid, else STUN alone.

Servers travel in the browser's ``RTCIceServer`` form (``{"urls", "username",
"credential"}``); ``for_webrtcbin`` turns them into webrtcbin's URIs.

A session can be put in a test mode (``MODES``, from the player's menu or
``?ice=`` on the page) to try one way of reaching the machine at a time;
``for_mode`` gives what the browser and the streamer then get.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DEFAULT_STUN = "stun:turn.merlincloud.dev:3478"
REFRESH_BEFORE_S = 3600  # fetch again when the credentials have less left
RETRY_AFTER_S = 60  # after a failed fetch, the next viewer may try again
FETCH_TIMEOUT_S = 3

TURN_OFF = "off"  # no Merlin Cloud: no relay to ask for

# Test modes: what each one leaves on. "auto" is everything.
MODES = {
    "auto": {"stun": True, "turn": True, "upnp": True},
    "direct": {"stun": False, "turn": False, "upnp": False},
    "upnp": {"stun": False, "turn": False, "upnp": True},
    "stun": {"stun": True, "turn": False, "upnp": False},
    "relay": {"stun": True, "turn": True, "upnp": True},  # the browser relays only
}
TURN_UNREACHABLE = "portal unreachable"


def stun_servers() -> list[dict]:
    """The STUN entry every stream gets (none when set to empty)."""
    url = os.environ.get("MERLIN_APP_STUN", DEFAULT_STUN).strip()
    return [{"urls": [url]}] if url else []


@dataclass
class Servers:
    """What a viewer's session gets: the servers, and why there is no relay
    when there is none (``turn`` is ``"ok"`` or a reason)."""

    ice_servers: list[dict] = field(default_factory=list)
    turn: str = TURN_OFF
    expires: float = 0.0  # time.time() of the TURN credentials' end, else 0

    def to_json(self) -> dict:
        return {"iceServers": self.ice_servers, "turn": self.turn}


def _valid(servers: list) -> list[dict]:
    """Keep the well-formed entries only (what the portal sent is data)."""
    out = []
    for entry in servers if isinstance(servers, list) else []:
        if not isinstance(entry, dict):
            continue
        urls = entry.get("urls")
        urls = [urls] if isinstance(urls, str) else urls
        if not isinstance(urls, list):
            continue
        urls = [
            u
            for u in urls
            if isinstance(u, str) and u.split(":", 1)[0] in ("stun", "turn", "turns")
        ]
        if not urls:
            continue
        clean: dict = {"urls": urls}
        for key in ("username", "credential"):
            if isinstance(entry.get(key), str):
                clean[key] = entry[key]
        out.append(clean)
    return out


class Cache:
    """The portal's answer, shared by the viewers of this Merlin."""

    def __init__(self, fetch=None, clock=time.time) -> None:
        self._fetch = fetch or fetch_from_portal
        self._clock = clock
        self._lock = threading.Lock()
        self._good: Servers | None = None
        self._failed_at: float | None = None

    def get(self) -> Servers:
        token = os.environ.get("MERLIN_SAAS_TOKEN", "").strip()
        if not token:
            return Servers(stun_servers(), TURN_OFF)
        with self._lock:
            now = self._clock()
            good = self._good
            if good is not None and good.expires - now > REFRESH_BEFORE_S:
                return good
            if self._failed_at is not None and now - self._failed_at < RETRY_AFTER_S:
                return self._fallback(now)
            try:
                fresh = self._fetch(token, now)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                self._failed_at = now
                return self._fallback(now)
            self._failed_at = None
            self._good = fresh
            return fresh

    def _fallback(self, now: float) -> Servers:
        good = self._good
        if good is not None and good.expires > now:
            return good
        return Servers(stun_servers(), TURN_UNREACHABLE)


def fetch_from_portal(token: str, now: float) -> Servers:
    api = os.environ.get("MERLIN_SAAS_API", "https://merlincloud.dev").rstrip("/")
    request = urllib.request.Request(
        f"{api}/api/instance/ice", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
        answer = json.loads(response.read().decode())
    if not isinstance(answer, dict):
        raise ValueError("not an object")
    # The relay comes from the portal; STUN stays ours (MERLIN_APP_STUN, empty
    # for none), whatever the portal lists.
    relays = [s for s in _valid(answer.get("iceServers")) if _is_turn(s)]
    turn = answer.get("turn")
    turn = turn if isinstance(turn, str) and turn else "?"
    ttl = answer.get("ttl")
    if not relays:
        return Servers(stun_servers(), turn if turn != "ok" else "?", 0.0)
    if not isinstance(ttl, (int, float)) or ttl <= 0:
        raise ValueError("credentials without a lifetime")
    return Servers(stun_servers() + relays, "ok", now + float(ttl))


def _is_turn(entry: dict) -> bool:
    return all(u.startswith(("turn:", "turns:")) for u in entry["urls"])


def for_mode(servers: Servers, mode: str) -> tuple[dict, dict]:
    """``(browser, streamer)`` settings for a session in ``mode`` (an
    unknown one is "auto"): the servers each end gets, why there is no
    relay, the browser's transport policy, and whether the streamer asks
    the router (UPnP)."""
    allowed = MODES.get(mode, MODES["auto"])
    mode = mode if mode in MODES else "auto"
    kept = [
        s
        for s in _valid(servers.ice_servers)
        if (allowed["turn"] if _is_turn(s) else allowed["stun"])
    ]
    turn = servers.turn if allowed["turn"] else f"off ({mode} test)"
    browser = {
        "iceServers": kept,
        "turn": turn,
        "policy": "relay" if mode == "relay" else "all",
        "mode": mode,
    }
    streamer = {"iceServers": kept, "turn": turn, "upnp": allowed["upnp"], "mode": mode}
    return browser, streamer


# --- for webrtcbin ----------------------------------------------------------------


def _host_port(rest: str) -> tuple[str, str]:
    """``host:port`` or ``[v6]:port`` after the scheme, query dropped."""
    rest = rest.split("?", 1)[0]
    if rest.startswith("["):
        host, _, tail = rest[1:].partition("]")
        port = tail.lstrip(":")
        return f"[{host}]", port
    host, _, port = rest.rpartition(":") if rest.count(":") == 1 else (rest, "", "")
    return host, port


def for_webrtcbin(servers: list[dict]) -> tuple[str | None, list[str]]:
    """``(stun_uri, turn_uris)`` for webrtcbin's ``stun-server`` property and
    ``add-turn-server`` signal: ``stun://host:port``,
    ``turn://user:pass@host:port?transport=udp|tcp``,
    ``turns://user:pass@host:port``. The user and password are percent-encoded
    (a TURN REST username carries a ``:``)."""
    stun = None
    turns: list[str] = []
    for entry in _valid(servers):
        user = urllib.parse.quote(entry.get("username", ""), safe="")
        password = urllib.parse.quote(entry.get("credential", ""), safe="")
        for url in entry["urls"]:
            scheme, _, rest = url.partition(":")
            query = urllib.parse.parse_qs(rest.split("?", 1)[1]) if "?" in rest else {}
            host, port = _host_port(rest)
            if not host:
                continue
            if scheme == "stun":
                stun = stun or f"stun://{host}:{port or 3478}"
            elif user and password:
                if scheme == "turns":
                    turns.append(f"turns://{user}:{password}@{host}:{port or 5349}")
                else:
                    transport = (query.get("transport") or ["udp"])[0]
                    turns.append(
                        f"turn://{user}:{password}@{host}:{port or 3478}"
                        f"?transport={transport}"
                    )
    return stun, turns
