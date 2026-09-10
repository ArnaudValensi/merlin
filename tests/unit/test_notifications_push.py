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
from notifications.push import PushSender, SubscriptionStore, VapidKeys, build_payload
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
            "url": "/terminal?target=alpha:@1",
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
