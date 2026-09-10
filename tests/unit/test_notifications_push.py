"""Tests for Web Push (notifications/push.py): the stores and their files,
the VAPID keys, the payload, the suppression rules and the sender with
pywebpush replaced by a recorder."""

import asyncio
import json
import os
import stat

import pytest

from board.sweep import Window
from notifications import push
from notifications.push import (
    PushSender,
    SubscriptionStore,
    VapidKeys,
    build_payload,
    encode_payload,
)
from notifications.watcher import Watcher


def event(
    sid="s1", state="done", wid="@1", session="alpha", cwd="/h/u/proj", name="claude"
):
    w = Watcher(lambda: [])
    (ev,) = w.observe(
        [
            Window(
                sid=sid,
                state=state,
                cwd=cwd,
                parent="",
                relation="",
                session=session,
                window_id=wid,
                index=1,
                active=False,
                activity=0,
                name=name,
            )
        ]
    )
    return ev


SUB = {
    "endpoint": "https://push.example/abc",
    "keys": {"p256dh": "BPUB", "auth": "AUTH"},
}


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class Recorder:
    """Stands in for pywebpush.webpush."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail  # endpoint -> exception

    def __call__(self, **kw):
        self.calls.append(kw)
        exc = (self.fail or {}).get(kw["subscription_info"]["endpoint"])
        if exc:
            raise exc


class FakeResponse:
    def __init__(self, status):
        self.status_code = status


class FakeWebPushException(Exception):
    def __init__(self, status):
        super().__init__(f"push failed {status}")
        self.response = FakeResponse(status)


class TestSubscriptionStore:
    def test_round_trip_and_mode(self, tmp_path):
        store = SubscriptionStore(
            tmp_path / "subscriptions.json", clock=lambda: 1_700_000_000.0
        )
        sub = store.add(SUB, "Pixel · Chrome")
        assert sub.label == "Pixel · Chrome"
        assert sub.created == "2023-11-14T22:13:20+00:00"
        assert mode_of(tmp_path / "subscriptions.json") == 0o600
        again = SubscriptionStore(tmp_path / "subscriptions.json")
        (loaded,) = again.all()
        assert loaded.endpoint == SUB["endpoint"]
        assert loaded.keys == SUB["keys"]
        assert loaded.info() == SUB
        assert "keys" not in loaded.public()

    def test_re_adding_keeps_created_and_updates_label(self, tmp_path):
        store = SubscriptionStore(tmp_path / "s.json", clock=lambda: 1.0)
        store.add(SUB, "first")
        store._clock = lambda: 2.0
        sub = store.add(SUB, "second")
        assert sub.label == "second"
        assert sub.created == "1970-01-01T00:00:01+00:00"
        assert len(store.all()) == 1

    def test_rejects_a_non_subscription(self, tmp_path):
        store = SubscriptionStore(tmp_path / "s.json")
        with pytest.raises(ValueError):
            store.add({"endpoint": "nope"}, "x")
        with pytest.raises(ValueError):
            store.add({"endpoint": "https://a/b", "keys": {"p256dh": "x"}}, "x")

    def test_remove(self, tmp_path):
        store = SubscriptionStore(tmp_path / "s.json")
        store.add(SUB, "a")
        assert store.remove(SUB["endpoint"]) is True
        assert store.remove(SUB["endpoint"]) is False
        assert store.all() == []

    def test_corrupt_file_is_kept_aside_and_store_starts_empty(self, tmp_path):
        path = tmp_path / "subscriptions.json"
        path.write_text("{not json")
        store = SubscriptionStore(path)
        assert store.all() == []
        aside = [
            p
            for p in tmp_path.iterdir()
            if p.name.startswith("subscriptions.json.corrupt-")
        ]
        assert len(aside) == 1
        assert aside[0].read_text() == "{not json"
        store.add(SUB, "a")  # and it is writable again
        assert (
            json.loads(path.read_text())["subscriptions"][SUB["endpoint"]]["label"]
            == "a"
        )

    def test_empty_file_is_an_empty_store(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("")
        assert SubscriptionStore(path).all() == []

    def test_mark_success(self, tmp_path):
        store = SubscriptionStore(tmp_path / "s.json", clock=lambda: 5.0)
        store.add(SUB, "a")
        store.mark_success(SUB["endpoint"])
        assert store.get(SUB["endpoint"]).last_success == "1970-01-01T00:00:05+00:00"


class TestVapidKeys:
    def test_generated_once_with_mode_0600(self, tmp_path):
        keys = VapidKeys(tmp_path / "vapid.json")
        public = keys.public_key
        assert public and "=" not in public and "+" not in public
        assert keys.private_pem.startswith("-----BEGIN PRIVATE KEY-----")
        assert mode_of(tmp_path / "vapid.json") == 0o600
        again = VapidKeys(tmp_path / "vapid.json")
        assert again.public_key == public
        assert again.private_pem == keys.private_pem

    def test_corrupt_key_file_is_regenerated(self, tmp_path):
        path = tmp_path / "vapid.json"
        path.write_text("garbage")
        keys = VapidKeys(path)
        assert keys.public_key
        assert any(p.name.startswith("vapid.json.corrupt-") for p in tmp_path.iterdir())


class TestPayload:
    def test_shape(self):
        p = build_payload(event(cwd="/home/u/merlin-saas", name="build"))
        assert p == {
            "title": "merlin-saas · build",
            "body": "Finished",
            "tag": "s1",
            "url": "/terminal?target=alpha%3A%401",
            "sid": "s1",
            "state": "done",
        }
        assert build_payload(event(state="ask"))["body"] == "Needs an answer"
        assert len(json.dumps(p)) < 3 * 1024


def make_sender(tmp_path, recorder=None, **kw):
    store = SubscriptionStore(tmp_path / "s.json", clock=lambda: 1.0)
    keys = VapidKeys(tmp_path / "vapid.json")
    return PushSender(store, keys, send=recorder or Recorder(), **kw), store


class TestSender:
    def test_send_passes_payload_ttl_urgency_and_vapid(self, tmp_path):
        rec = Recorder()
        sender, store = make_sender(tmp_path, rec)
        store.add(SUB, "a")
        result = asyncio.run(sender.deliver(event()))
        assert result.sent == 1 and result.failed == 0
        (call,) = rec.calls
        assert call["subscription_info"] == SUB
        assert call["ttl"] == 300
        assert call["headers"] == {"Urgency": "high"}
        assert call["vapid_claims"] == {"sub": push.VAPID_SUBJECT}
        from py_vapid import Vapid

        assert isinstance(call["vapid_private_key"], Vapid)
        assert (
            call["vapid_private_key"].private_pem() == sender.keys.private_pem.encode()
        )
        assert json.loads(call["data"])["title"] == "proj · claude"
        assert store.get(SUB["endpoint"]).last_success

    def test_410_and_404_remove_the_subscription(self, tmp_path):
        gone = {"endpoint": "https://push.example/gone", "keys": SUB["keys"]}
        missing = {"endpoint": "https://push.example/missing", "keys": SUB["keys"]}
        rec = Recorder(
            fail={
                gone["endpoint"]: FakeWebPushException(410),
                missing["endpoint"]: FakeWebPushException(404),
            }
        )
        sender, store = make_sender(tmp_path, rec)
        store.add(SUB, "ok")
        store.add(gone, "gone")
        store.add(missing, "missing")
        result = asyncio.run(sender.deliver(event()))
        assert (result.sent, result.removed, result.failed) == (1, 2, 0)
        assert [s.endpoint for s in store.all()] == [SUB["endpoint"]]

    def test_other_failures_are_logged_and_kept(self, tmp_path, monkeypatch):
        logged = []
        import structured_log

        monkeypatch.setattr(
            structured_log, "log_event", lambda t, **f: logged.append((t, f))
        )
        rec = Recorder(fail={SUB["endpoint"]: FakeWebPushException(500)})
        sender, store = make_sender(tmp_path, rec)
        store.add(SUB, "a")
        result = asyncio.run(sender.deliver(event()))
        assert result.failed == 1
        assert store.get(SUB["endpoint"]) is not None
        assert (
            logged and logged[0][0] == "push_failed" and logged[0][1]["status"] == 500
        )

    def test_no_subscriptions_sends_nothing(self, tmp_path):
        rec = Recorder()
        sender, _ = make_sender(tmp_path, rec)
        assert asyncio.run(sender.deliver(event())).skipped == "no subscriptions"
        assert rec.calls == []

    def test_send_all_only_some_endpoints(self, tmp_path):
        rec = Recorder()
        sender, store = make_sender(tmp_path, rec)
        other = {"endpoint": "https://push.example/other", "keys": SUB["keys"]}
        store.add(SUB, "a")
        store.add(other, "b")
        result = asyncio.run(sender.send_all({"title": "t"}, only=[other["endpoint"]]))
        assert result.sent == 1
        assert rec.calls[0]["subscription_info"]["endpoint"] == other["endpoint"]


class TestSuppression:
    def test_displayed_window_gets_no_push(self, tmp_path):
        rec = Recorder()
        sender, store = make_sender(
            tmp_path, rec, is_displayed=lambda t: t == "alpha:@1"
        )
        store.add(SUB, "a")
        assert asyncio.run(sender.deliver(event())).skipped == "displayed"
        assert rec.calls == []
        assert asyncio.run(sender.deliver(event(wid="@2"))).sent == 1

    def test_same_sid_within_20_seconds_gets_one_push(self, tmp_path):
        now = [1000.0]
        rec = Recorder()
        sender, store = make_sender(tmp_path, rec, clock=lambda: now[0])
        store.add(SUB, "a")
        assert asyncio.run(sender.deliver(event())).sent == 1
        now[0] += 5
        assert asyncio.run(sender.deliver(event(state="ask"))).skipped == "recent"
        now[0] += 15  # 20 seconds after the first push
        assert asyncio.run(sender.deliver(event())).sent == 1
        assert len(rec.calls) == 2

    def test_rate_limit_is_per_sid(self, tmp_path):
        rec = Recorder()
        sender, store = make_sender(tmp_path, rec, clock=lambda: 1000.0)
        store.add(SUB, "a")
        assert asyncio.run(sender.deliver(event(sid="a"))).sent == 1
        assert asyncio.run(sender.deliver(event(sid="b", wid="@2"))).sent == 1

    def test_a_suppressed_event_does_not_arm_the_rate_limit(self, tmp_path):
        shown = {"alpha:@1"}
        rec = Recorder()
        sender, store = make_sender(
            tmp_path, rec, is_displayed=lambda t: t in shown, clock=lambda: 1000.0
        )
        store.add(SUB, "a")
        assert asyncio.run(sender.deliver(event())).skipped == "displayed"
        shown.clear()
        assert asyncio.run(sender.deliver(event())).sent == 1


# ---------------------------------------------------------------------------
# Review round: concurrency, semantic corruption, payload bounds
# ---------------------------------------------------------------------------

import threading  # noqa: E402


def run_threads(n, fn):
    errors, results = [], []
    start = threading.Barrier(n)

    def body(i):
        try:
            start.wait(5)
            results.append(fn(i))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=body, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return results, errors


class TestConcurrency:
    def test_first_use_of_the_keys_from_many_threads_agrees_on_one_pair(self, tmp_path):
        keys = VapidKeys(tmp_path / "vapid.json")
        results, errors = run_threads(32, lambda _i: keys.public_key)
        assert errors == []
        assert len(set(results)) == 1
        assert (
            json.loads((tmp_path / "vapid.json").read_text())["public_key"]
            == results[0]
        )
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

    def test_concurrent_adds_keep_every_device_and_a_valid_file(self, tmp_path):
        store = SubscriptionStore(tmp_path / "subscriptions.json")

        def add(i):
            return store.add(
                {"endpoint": f"https://push.example/{i}", "keys": SUB["keys"]}, f"d{i}"
            ).endpoint

        results, errors = run_threads(64, add)
        assert errors == []
        assert len(set(results)) == 64
        data = json.loads((tmp_path / "subscriptions.json").read_text())
        assert len(data["subscriptions"]) == 64
        assert len(SubscriptionStore(tmp_path / "subscriptions.json").all()) == 64
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

    def test_concurrent_add_remove_and_success_never_corrupt(self, tmp_path):
        store = SubscriptionStore(tmp_path / "s.json")
        for i in range(8):
            store.add(
                {"endpoint": f"https://push.example/{i}", "keys": SUB["keys"]}, "x"
            )

        def op(i):
            ep = f"https://push.example/{i % 8}"
            if i % 3 == 0:
                store.mark_success(ep)
            elif i % 3 == 1:
                store.remove(ep)
            else:
                store.add({"endpoint": ep, "keys": SUB["keys"]}, "y")
            return True

        _, errors = run_threads(48, op)
        assert errors == []
        json.loads((tmp_path / "s.json").read_text())  # still valid JSON
        assert SubscriptionStore(tmp_path / "s.json").all() is not None


class TestSemanticCorruption:
    @pytest.mark.parametrize(
        "text", ['{"subscriptions": []}', "null", "[1, 2]", '"x"', "{}"]
    )
    def test_wrong_shapes_recover(self, tmp_path, text):
        path = tmp_path / "subscriptions.json"
        path.write_text(text)
        store = SubscriptionStore(path)
        assert store.all() == []
        store.add(SUB, "a")
        assert (
            json.loads(path.read_text())["subscriptions"][SUB["endpoint"]]["label"]
            == "a"
        )
        if text != "{}":
            assert any(
                p.name.startswith("subscriptions.json.corrupt-")
                for p in tmp_path.iterdir()
            )

    def test_entries_of_the_wrong_shape_are_skipped(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(
            json.dumps(
                {"subscriptions": {"https://a/b": 5, "https://a/c": {"keys": {}}}}
            )
        )
        assert SubscriptionStore(path).all() == []

    @pytest.mark.parametrize(
        "text",
        ['{"public_key": "x"}', '{"private_pem": "nope", "public_key": "x"}', "[]"],
    )
    def test_key_file_of_the_wrong_shape_is_regenerated(self, tmp_path, text):
        path = tmp_path / "vapid.json"
        path.write_text(text)
        keys = VapidKeys(path)
        assert keys.private_pem.startswith("-----BEGIN PRIVATE KEY-----")
        assert any(p.name.startswith("vapid.json.corrupt-") for p in tmp_path.iterdir())


class TestPayloadBounds:
    def test_long_unicode_names_yield_complete_json_under_the_limit(self):
        ev = event(cwd="/h/" + "é" * 3000, name="ü" * 3000, sid="s" * 500)
        data = encode_payload(build_payload(ev))
        assert len(data.encode("utf-8")) < 3 * 1024
        parsed = json.loads(data)
        assert parsed["url"] == "/terminal?target=alpha%3A%401"
        assert parsed["title"].endswith("…") and len(parsed["title"]) <= 120
        assert parsed["state"] == "done"

    def test_oversized_url_is_shortened_structurally(self):
        data = encode_payload(
            {"title": "t", "body": "b", "url": "/x?" + "a" * 5000, "state": "done"}
        )
        assert len(data.encode()) < 3 * 1024
        json.loads(data)

    def test_target_with_query_delimiters_is_encoded(self):
        payload = build_payload(event(session="a&b#c=d"))
        assert payload["url"] == "/terminal?target=a%26b%23c%3Dd%3A%401"
        from urllib.parse import parse_qs, urlsplit

        assert parse_qs(urlsplit(payload["url"]).query)["target"] == ["a&b#c=d:@1"]

    def test_send_one_sends_the_bounded_payload(self, tmp_path):
        rec = Recorder()
        sender, store = make_sender(tmp_path, rec)
        store.add(SUB, "a")
        asyncio.run(sender.deliver(event(name="n" * 5000)))
        data = rec.calls[0]["data"]
        assert len(data.encode("utf-8")) < 3 * 1024
        assert json.loads(data)["url"] == "/terminal?target=alpha%3A%401"


class TestAsyncEntryPoints:
    def test_deliver_reads_the_store_off_the_loop(self, tmp_path, monkeypatch):
        rec = Recorder()
        sender, store = make_sender(tmp_path, rec)
        store.add(SUB, "a")
        seen = []
        original = sender.store.all

        def spy():
            seen.append(threading.current_thread() is threading.main_thread())
            return original()

        monkeypatch.setattr(sender.store, "all", spy)
        assert asyncio.run(sender.deliver(event())).sent == 1
        assert seen and not any(seen)

    def test_test_sync_outcomes(self, tmp_path):
        rec = Recorder()
        sender, store = make_sender(tmp_path, rec)
        assert sender.test_sync({"title": "t"}) == "none"
        store.add(SUB, "a")
        assert (
            sender.test_sync({"title": "t"}, "https://push.example/nope") == "unknown"
        )
        result = sender.test_sync({"title": "t"}, SUB["endpoint"])
        assert not isinstance(result, str) and result.sent == 1


class TestAtomicSuppression:
    def test_concurrent_deliveries_for_one_sid_send_once(self, tmp_path, monkeypatch):
        rec = Recorder()
        sender, store = make_sender(tmp_path, rec, clock=lambda: 1000.0)
        store.add(SUB, "a")
        # Both preflights leave their threads together, so both reach the
        # suppression step believing nothing was pushed yet.
        gate = threading.Barrier(2)
        original = sender.store.all
        preflights = [0]

        def all_with_gate():
            # Only the two preflight reads meet at the gate. The later read
            # inside the send itself passes straight through.
            preflights[0] += 1
            if preflights[0] <= 2:
                gate.wait(5)
            return original()

        monkeypatch.setattr(sender.store, "all", all_with_gate)

        async def both():
            return await asyncio.gather(
                sender.deliver(event()), sender.deliver(event(state="ask"))
            )

        results = asyncio.run(both())
        assert sorted((r.sent, r.skipped) for r in results) == [(0, "recent"), (1, "")]
        assert len(rec.calls) == 1

    def test_concurrent_suppressed_event_does_not_arm_the_limit(self, tmp_path):
        rec = Recorder()
        sender, store = make_sender(
            tmp_path, rec, is_displayed=lambda t: t == "alpha:@9", clock=lambda: 1000.0
        )
        store.add(SUB, "a")

        async def both():
            return await asyncio.gather(
                sender.deliver(event(wid="@9")), sender.deliver(event(wid="@1"))
            )

        results = asyncio.run(both())
        assert sorted((r.sent, r.skipped) for r in results) == [
            (0, "displayed"),
            (1, ""),
        ]
        # The displayed one reserved nothing: a later event for the sid still sends
        # only because the sent one reserved it. Check the reservation belongs to
        # the sent event by moving the clock past the window.
        sender._clock = lambda: 1030.0
        assert asyncio.run(sender.deliver(event(wid="@1"))).sent == 1

    def test_reserve_is_atomic_across_threads(self, tmp_path):
        rec = Recorder()
        sender, _ = make_sender(tmp_path, rec, clock=lambda: 5.0)
        results, errors = run_threads(16, lambda _i: sender.reserve(event()))
        assert errors == []
        assert results.count("") == 1 and results.count("recent") == 15
