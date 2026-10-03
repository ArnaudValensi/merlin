"""The ICE servers a stream uses (app/iceservers.py, pure): the STUN
setting, the portal's TURN credentials and their cache, webrtcbin's URIs."""

import io
import json
import urllib.error

import pytest

from app import iceservers
from app.iceservers import Cache, Servers

TURN = {
    "urls": [
        "turn:turn.merlincloud.dev:3478?transport=udp",
        "turns:turn.merlincloud.dev:443?transport=tcp",
    ],
    "username": "1790900000:lisa",
    "credential": "c3VwZXI/c2VjcmV0+/=",
}
STUN = {"urls": ["stun:turn.merlincloud.dev:3478"]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("MERLIN_APP_STUN", raising=False)
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    monkeypatch.delenv("MERLIN_SAAS_API", raising=False)


def test_every_merlin_gets_our_stun_unless_told_otherwise(monkeypatch):
    assert iceservers.stun_servers() == [{"urls": [iceservers.DEFAULT_STUN]}]
    monkeypatch.setenv("MERLIN_APP_STUN", "stun:stun.example.org:3478")
    assert iceservers.stun_servers() == [{"urls": ["stun:stun.example.org:3478"]}]
    monkeypatch.setenv("MERLIN_APP_STUN", "")
    assert iceservers.stun_servers() == []


def test_without_merlin_cloud_there_is_no_relay_and_no_call():
    calls = []
    cache = Cache(fetch=lambda token, now: calls.append(token))
    servers = cache.get()
    assert servers.ice_servers == [STUN]
    assert servers.turn == iceservers.TURN_OFF
    assert calls == []


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def _fetcher(answers):
    calls = []

    def fetch(token, now):
        calls.append(now)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return fetch, calls


def test_credentials_are_cached_until_an_hour_before_they_end(monkeypatch):
    monkeypatch.setenv("MERLIN_SAAS_TOKEN", "mrl_x")
    clock = Clock()
    first = Servers([STUN, TURN], "ok", clock.now + 6 * 3600)
    second = Servers([STUN, TURN], "ok", clock.now + 12 * 3600)
    fetch, calls = _fetcher([first, second])
    cache = Cache(fetch=fetch, clock=clock)
    assert cache.get() is first
    clock.now += 4 * 3600  # two hours left: still good
    assert cache.get() is first
    clock.now += 1.5 * 3600  # half an hour left: fetch again
    assert cache.get() is second
    assert len(calls) == 2


def test_a_down_portal_keeps_valid_credentials_then_falls_back_to_stun(monkeypatch):
    monkeypatch.setenv("MERLIN_SAAS_TOKEN", "mrl_x")
    clock = Clock()
    good = Servers([STUN, TURN], "ok", clock.now + 6 * 3600)
    down = urllib.error.URLError("refused")
    fetch, calls = _fetcher([good, down, down])
    cache = Cache(fetch=fetch, clock=clock)
    cache.get()
    clock.now += 5.5 * 3600  # due for a refresh, the portal is down
    assert cache.get() is good  # still valid: kept
    clock.now += 30  # within the retry pause: no new call
    assert cache.get() is good
    assert len(calls) == 2
    clock.now += 0.5 * 3600  # expired, the portal still down
    fallback = cache.get()
    assert fallback.ice_servers == [STUN]
    assert fallback.turn == iceservers.TURN_UNREACHABLE
    assert len(calls) == 3


def _portal(monkeypatch, answer, status=200):
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "no", {}, None)
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(iceservers.urllib.request, "urlopen", urlopen)
    return seen


def test_the_portal_is_asked_with_the_instance_token(monkeypatch):
    seen = _portal(
        monkeypatch, {"iceServers": [STUN, TURN], "ttl": 21600, "turn": "ok"}
    )
    monkeypatch.setenv("MERLIN_SAAS_API", "https://merlincloud.test/")
    servers = iceservers.fetch_from_portal("mrl_abc", 100.0)
    assert seen == {
        "url": "https://merlincloud.test/api/instance/ice",
        "auth": "Bearer mrl_abc",
    }
    assert servers.ice_servers == [STUN, TURN]
    assert servers.turn == "ok"
    assert servers.expires == 100.0 + 21600


def test_an_account_without_access_gets_stun_and_the_reason(monkeypatch):
    _portal(monkeypatch, {"iceServers": [STUN], "ttl": 21600, "turn": "no access"})
    servers = iceservers.fetch_from_portal("mrl_abc", 100.0)
    assert servers.ice_servers == [STUN]
    assert servers.turn == "no access"
    assert servers.expires == 0.0


@pytest.mark.parametrize(
    "answer",
    [
        ["not", "an", "object"],
        {"iceServers": [STUN, TURN], "turn": "ok"},  # no lifetime
        {"iceServers": [STUN, TURN], "ttl": -5, "turn": "ok"},
    ],
)
def test_a_malformed_answer_is_a_failed_fetch(monkeypatch, answer):
    _portal(monkeypatch, answer)
    with pytest.raises(ValueError):
        iceservers.fetch_from_portal("mrl_abc", 100.0)


def test_odd_entries_from_the_portal_are_dropped():
    servers = [
        STUN,
        "nonsense",
        {"urls": "turn:turn.example:3478", "username": "u", "credential": "p"},
        {"urls": ["http://evil.example/"]},
        {"urls": [42]},
        {"no": "urls"},
    ]
    assert iceservers._valid(servers) == [
        STUN,
        {"urls": ["turn:turn.example:3478"], "username": "u", "credential": "p"},
    ]


def test_webrtcbin_gets_uris_with_the_credentials_escaped():
    stun, turns = iceservers.for_webrtcbin([STUN, TURN])
    assert stun == "stun://turn.merlincloud.dev:3478"
    assert turns == [
        "turn://1790900000%3Alisa:c3VwZXI%2Fc2VjcmV0%2B%2F%3D"
        "@turn.merlincloud.dev:3478?transport=udp",
        "turns://1790900000%3Alisa:c3VwZXI%2Fc2VjcmV0%2B%2F%3D"
        "@turn.merlincloud.dev:443",
    ]


def test_webrtcbin_uris_for_bare_and_ipv6_hosts():
    stun, turns = iceservers.for_webrtcbin(
        [
            {"urls": ["stun:[2001:db8::1]:3478"]},
            {"urls": ["turn:[2001:db8::2]"], "username": "u", "credential": "p"},
        ]
    )
    assert stun == "stun://[2001:db8::1]:3478"
    assert turns == ["turn://u:p@[2001:db8::2]:3478?transport=udp"]


def test_a_turn_entry_without_credentials_is_not_used():
    stun, turns = iceservers.for_webrtcbin(
        [{"urls": ["turn:turn.example:3478"]}, {"urls": []}]
    )
    assert stun is None
    assert turns == []
