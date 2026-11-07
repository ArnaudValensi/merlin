"""Workspace files, history, crash points and the startup freeze
(workspace/store.py, service.py)."""

import json
import time

import pytest

from workspace import service, store
from workspace import snapshot as snap


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MERLIN_HOME", str(tmp_path / "merlin"))
    return tmp_path


def make(*names, saved_at=1.0):
    return {
        "version": 1,
        "saved_at": saved_at,
        "sessions": [
            {
                "name": n,
                "active_window": 0,
                "windows": [
                    {
                        "index": 0,
                        "name": "w",
                        "layout": "",
                        "options": {},
                        "panes": [{"index": 0, "cwd": "/"}],
                    }
                ],
            }
            for n in names
        ],
    }


def live(monkeypatch, names, fillable=()):
    from workspace import restore as rs

    state = None if names is None else {n: n in fillable for n in names}
    monkeypatch.setattr(rs, "live_sessions", lambda: state)


def offer():
    return store.read_snapshot(store.pending_path())


def without_crash(snapshot):
    return {k: v for k, v in snapshot.items() if k not in ("crash", "id")}


def crash_files():
    return sorted(p.name for p in store.crashes_dir().glob("*.json"))


def history_files():
    return sorted(p.name for p in store.history_dir().glob("*.json"))


class TestFiles:
    def test_round_trip_and_no_temp_left(self):
        store.write_snapshot(store.latest_path(), make("a"))
        assert store.read_snapshot(store.latest_path()) == make("a")
        assert [p.name for p in store.latest_path().parent.iterdir()] == ["latest.json"]

    def test_torn_or_foreign_file_reads_as_absent(self):
        path = store.latest_path()
        path.parent.mkdir(parents=True)
        path.write_text('{"version": 1, "sess')
        assert store.read_snapshot(path) is None
        path.write_text(json.dumps({"version": 99, "sessions": []}))
        assert store.read_snapshot(path) is None
        path.write_text(json.dumps({"version": 1, "sessions": [{"name": 3}]}))
        assert store.read_snapshot(path) is None

    @pytest.mark.parametrize(
        "saved_at",
        [
            "invalid",
            {"t": 1},
            None,
            True,
            float("nan"),
            float("inf"),
            -1,
            2**40,
            json.loads("1" + "0" * 400),  # a JSON int too big for a float
        ],
    )
    def test_a_bad_save_time_reads_as_absent(self, saved_at):
        # The id of a crash point is computed from saved_at: a value that
        # localtime cannot take must never reach it, nor stall the freeze.
        path = store.latest_path()
        path.parent.mkdir(parents=True)
        data = make("a")
        if saved_at is None:
            del data["saved_at"]
        else:
            data["saved_at"] = saved_at
        path.write_text(json.dumps(data))
        assert store.read_snapshot(path) is None
        assert store.valid_time(saved_at) is None


class TestFreeze:
    def test_missing_sessions_freeze_an_offer(self, monkeypatch):
        store.write_snapshot(store.latest_path(), make("a", "b"))
        live(monkeypatch, None)  # no tmux server: after a reboot
        assert service.freeze_pending(now=77.0) is True
        pending = offer()
        assert without_crash(pending) == make("a", "b")
        # The offer is a crash point first, with reason startup, never pushed.
        assert pending["crash"] == {"reason": "startup", "detected_at": 77.0}
        assert crash_files() == [pending["id"] + ".json"]
        point = store.read_snapshot(store.entry_path(pending["id"]))
        assert point == {**make("a", "b"), "crash": pending["crash"]}

    def test_nothing_missing_no_offer(self, monkeypatch):
        # Merlin restarted for an update: tmux kept running.
        store.write_snapshot(store.latest_path(), make("a", "b"))
        live(monkeypatch, ["a", "b", "c"])
        assert service.freeze_pending() is False
        assert not store.pending_path().exists()

    def test_terminal_placeholder_does_not_hide_a_lost_default_session(
        self, monkeypatch
    ):
        # A tab reconnecting in the instant Merlin starts creates a bare
        # merlin-dev before the freeze. The saved merlin-dev had more.
        rich = make("merlin-dev")
        rich["sessions"][0]["windows"].append(
            dict(rich["sessions"][0]["windows"][0], index=1)
        )
        store.write_snapshot(store.latest_path(), rich)
        live(monkeypatch, ["merlin-dev"], fillable=["merlin-dev"])
        assert service.freeze_pending() is True
        assert without_crash(offer()) == rich

    def test_bare_default_session_saved_bare_is_no_offer(self, monkeypatch):
        # Nothing to bring back: one shell saved, one shell live.
        store.write_snapshot(store.latest_path(), make("merlin-dev"))
        live(monkeypatch, ["merlin-dev"], fillable=["merlin-dev"])
        assert service.freeze_pending() is False

    def test_existing_offer_is_kept(self, monkeypatch):
        store.write_snapshot(store.pending_path(), make("old"))
        store.write_snapshot(store.latest_path(), make("new"))
        live(monkeypatch, None)
        assert service.freeze_pending() is True
        assert offer() == make("old")
        assert crash_files() == []  # nothing new was frozen

    def test_no_snapshot_no_offer(self, monkeypatch):
        live(monkeypatch, None)
        assert service.freeze_pending() is False


class TestSweep:
    def test_writes_only_on_change(self, monkeypatch):
        shots = iter(
            [make("a", saved_at=1), make("a", saved_at=2), make("a", "b", saved_at=3)]
        )
        monkeypatch.setattr(snap, "take_snapshot", lambda: next(shots))
        last = service.sweep_once(None)
        assert last["saved_at"] == 1
        last = service.sweep_once(last)
        assert store.read_snapshot(store.latest_path())["saved_at"] == 1  # unchanged
        last = service.sweep_once(last)
        assert store.read_snapshot(store.latest_path())["saved_at"] == 3

    @pytest.mark.parametrize(
        "shot", [None, {"version": 1, "saved_at": 5, "sessions": []}]
    )
    def test_never_overwrites_with_no_server_or_empty(self, monkeypatch, shot):
        store.write_snapshot(store.latest_path(), make("a"))
        monkeypatch.setattr(snap, "take_snapshot", lambda: shot)
        service.sweep_once(make("a"))
        assert store.read_snapshot(store.latest_path()) == make("a")

    @pytest.mark.asyncio
    async def test_run_freezes_before_first_write(self, monkeypatch):
        # Right after a reboot: the latest snapshot holds sessions, tmux has a
        # fresh default session only. The offer must survive the first sweep.
        import asyncio

        store.write_snapshot(store.latest_path(), make("main", "portal"))
        live(monkeypatch, ["merlin-dev"])
        monkeypatch.setattr(
            snap, "take_snapshot", lambda: make("merlin-dev", saved_at=9)
        )
        stop = asyncio.Event()
        task = asyncio.create_task(service.run(stop, interval=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await task
        assert without_crash(offer()) == make("main", "portal")
        assert (
            store.read_snapshot(store.latest_path())["sessions"][0]["name"]
            == "merlin-dev"
        )

    @pytest.mark.asyncio
    async def test_failed_freeze_blocks_writes_until_it_succeeds(self, monkeypatch):
        import asyncio

        store.write_snapshot(store.latest_path(), make("main"))
        attempts = []

        def flaky_freeze():
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("no space left on device")
            return service.__dict__["_real_freeze"]()

        service.__dict__["_real_freeze"] = service.freeze_pending
        monkeypatch.setattr(service, "freeze_pending", flaky_freeze)
        live(monkeypatch, None)
        monkeypatch.setattr(
            snap, "take_snapshot", lambda: make("merlin-dev", saved_at=9)
        )
        stop = asyncio.Event()
        task = asyncio.create_task(service.run(stop, interval=0.01))
        while len(attempts) < 2:
            await asyncio.sleep(0.001)
        # Still failing: the only copy of the restore point is untouched.
        assert store.read_snapshot(store.latest_path()) == make("main")
        while (
            len(attempts) < 3
            or store.read_snapshot(store.latest_path())["saved_at"] != 9
        ):
            await asyncio.sleep(0.005)
        stop.set()
        await task
        assert without_crash(offer()) == make("main")
        del service.__dict__["_real_freeze"]

    @pytest.mark.asyncio
    async def test_a_foreign_latest_never_stalls_the_sweep(self, monkeypatch):
        # A latest.json with a save time the freeze cannot turn into an id
        # reads as absent: no offer, and the sweep writes from the first turn.
        import asyncio

        path = store.latest_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({**make("main"), "saved_at": "yesterday"}))
        live(monkeypatch, None)
        monkeypatch.setattr(
            snap, "take_snapshot", lambda: make("merlin-dev", saved_at=9)
        )
        stop = asyncio.Event()
        task = asyncio.create_task(service.run(stop, interval=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await task
        assert store.read_snapshot(store.latest_path())["saved_at"] == 9
        assert not store.pending_path().exists()

    def test_every_write_keeps_a_history_copy(self, monkeypatch):
        # Recent save times: the thinning that follows each write keeps them.
        t = time.time() - 60
        shots = iter(
            [
                make("a", saved_at=t),
                make("a", saved_at=t + 1),
                make("a", "b", saved_at=t + 2),
            ]
        )
        monkeypatch.setattr(snap, "take_snapshot", lambda: next(shots))
        last = service.sweep_once(None)
        last = service.sweep_once(last)  # unchanged: no write, no copy
        service.sweep_once(last)
        ids = [n[: -len(".json")] for n in history_files()]
        assert len(ids) == 2
        assert [store.read_snapshot(store.entry_path(i))["saved_at"] for i in ids] == [
            t,
            t + 2,
        ]

    def test_history_failure_never_blocks_the_sweep(self, monkeypatch):
        monkeypatch.setattr(snap, "take_snapshot", lambda: make("a", saved_at=1))
        monkeypatch.setattr(
            store, "write_history", lambda s: (_ for _ in ()).throw(OSError("full"))
        )
        last = service.sweep_once(None)
        assert last["saved_at"] == 1  # the snapshot advanced all the same
        assert store.read_snapshot(store.latest_path()) == make("a", saved_at=1)


class TestPendingInfo:
    def test_lists_only_sessions_still_missing(self, monkeypatch):
        from workspace import restore as rs

        snapshot = make("a", "b", "c")
        b = snapshot["sessions"][1]
        b["windows"].append(dict(b["windows"][0], index=1))  # b had more than one shell
        store.write_snapshot(store.pending_path(), snapshot)
        monkeypatch.setattr(rs, "live_sessions", lambda: {"a": False, "b": True})
        info = service.pending_info()
        assert info["pending"] is True
        assert [s["name"] for s in info["sessions"]] == ["b", "c"]  # b is fillable

    def test_offer_dropped_when_nothing_missing(self, monkeypatch):
        from workspace import restore as rs

        store.write_snapshot(store.pending_path(), make("a"))
        monkeypatch.setattr(rs, "live_sessions", lambda: {"a": False})
        assert service.pending_info() == {"pending": False}
        assert not store.pending_path().exists()

    def test_dismiss_drops_the_offer(self):
        store.write_snapshot(store.pending_path(), make("a"))
        service.dismiss_pending()
        assert service.pending_info() == {"pending": False}


class TestRestorePending:
    def _result(self, failed):
        return {"restored": ["a"], "skipped": [], "failed": failed, "agents": 0}

    def test_a_complete_restore_drops_the_offer(self, monkeypatch):
        from workspace import restore as rs

        store.write_snapshot(store.pending_path(), make("a", "b"))
        monkeypatch.setattr(rs, "restore", lambda s: self._result([]))
        assert service.restore_pending()["restored"] == ["a"]
        assert not store.pending_path().exists()

    def test_a_failed_restore_keeps_the_offer(self, monkeypatch):
        from workspace import restore as rs

        store.write_snapshot(store.pending_path(), make("a", "b"))
        monkeypatch.setattr(rs, "restore", lambda s: self._result(["b"]))
        assert service.restore_pending()["failed"] == ["b"]
        assert offer() == make("a", "b")

    def test_a_restore_that_raises_keeps_the_offer(self, monkeypatch):
        from workspace import restore as rs

        store.write_snapshot(store.pending_path(), make("a"))

        def boom(s):
            raise RuntimeError("tmux vanished")

        monkeypatch.setattr(rs, "restore", boom)
        with pytest.raises(RuntimeError):
            service.restore_pending()
        assert offer() == make("a")


class TestIds:
    def test_id_is_the_local_save_time(self, monkeypatch):
        monkeypatch.setenv("TZ", "UTC")
        time.tzset()
        try:
            saved = time.mktime((2026, 11, 5, 18, 59, 45, 0, 0, -1))
            assert store.new_id(saved) == "20261105-185945"
            assert store.id_time("20261105-185945") == saved
            assert store.id_time("20261105-185945-3") == saved
            assert store.id_time("latest") is None
        finally:
            monkeypatch.delenv("TZ")
            time.tzset()

    def test_collisions_are_suffixed_across_history_and_crashes(self):
        shot = make("a", saved_at=1000.0)
        first = store.write_history(shot)
        second = store.write_history(shot)
        third, _ = store.write_crash(shot, "server-gone", 1001.0)
        assert second == first + "-2"
        assert third == first + "-3"
        assert store.entry_path(first) == store.history_dir() / f"{first}.json"
        assert store.entry_path(third) == store.crashes_dir() / f"{third}.json"
        assert store.entry_path("nope") is None
        assert store.entry_path("../latest") is None

    def test_a_crash_point_carries_its_crash_object(self):
        shot = make("a", saved_at=1000.0)
        entry_id, frozen = store.write_crash(shot, "server-changed", 1234.5)
        assert frozen["crash"] == {"reason": "server-changed", "detected_at": 1234.5}
        assert store.read_snapshot(store.entry_path(entry_id)) == frozen
        assert store.crash_of(frozen) == frozen["crash"]
        assert store.crash_of(shot) is None
        huge = json.loads("1" + "0" * 400)
        for bad in ("soon", huge, float("nan"), None):
            crash = {"reason": "x", "detected_at": bad}
            assert store.crash_of({**shot, "crash": crash}) is None
        assert (
            store.crash_of({**shot, "crash": {"reason": 1, "detected_at": 5.0}}) is None
        )


class TestServerIdentity:
    def test_read_from_snapshot_optional_and_validated(self):
        assert store.server_of(make("a")) is None
        assert store.server_of(
            {**make("a"), "server": {"pid": 5, "start_time": 9}}
        ) == (
            5,
            9,
        )
        assert store.server_of({**make("a"), "server": {"pid": "5"}}) is None
        assert store.server_of({**make("a"), "server": "x"}) is None

    def test_old_snapshot_without_server_still_reads(self):
        store.write_snapshot(store.latest_path(), make("a"))
        assert store.read_snapshot(store.latest_path()) == make("a")
        with_server = {**make("a"), "server": {"pid": 1, "start_time": 2}}
        store.write_snapshot(store.latest_path(), with_server)
        assert store.read_snapshot(store.latest_path()) == with_server


class TestThinning:
    @pytest.fixture(autouse=True)
    def utc(self, monkeypatch):
        monkeypatch.setenv("TZ", "UTC")
        time.tzset()
        yield
        monkeypatch.delenv("TZ")
        time.tzset()

    def _plant(self, directory, now, *ages):
        ids = []
        for age in ages:
            entry_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now - age))
            (directory / f"{entry_id}.json").write_text("{}")
            ids.append(entry_id)
        return ids

    def test_keeps_two_hours_then_hourly_to_seven_days(self):
        now = time.mktime((2026, 11, 7, 12, 0, 0, 0, 0, -1))
        h, d = 3600, 86400
        store.history_dir().mkdir(parents=True)
        ids = self._plant(
            store.history_dir(),
            now,
            10,  # kept: under two hours
            h + 59 * 60,  # kept: under two hours
            2 * h + 10 * 60,  # 09:50, the newest of hour 09: kept
            2 * h + 40 * 60,  # 09:20, same hour: dropped
            3 * d,  # alone in its hour: kept
            7 * d - 1,  # alone in its hour, under seven days: kept
            7 * d,  # seven days: dropped
            9 * d,  # dropped
        )
        (store.history_dir() / "notes.json").write_text("{}")  # foreign: untouched
        removed = store.thin(now)
        assert sorted(p.stem for p in removed) == sorted([ids[3], ids[6], ids[7]])
        assert history_files() == sorted(
            [f"{i}.json" for n, i in enumerate(ids) if n not in (3, 6, 7)]
            + ["notes.json"]
        )

    def test_a_suffixed_entry_is_the_newest_of_its_second(self):
        now = time.mktime((2026, 11, 7, 12, 0, 0, 0, 0, -1))
        store.history_dir().mkdir(parents=True)
        (base,) = self._plant(store.history_dir(), now, 3 * 3600)
        (store.history_dir() / f"{base}-2.json").write_text("{}")
        store.thin(now)
        assert history_files() == [f"{base}-2.json"]

    def test_crash_points_go_after_thirty_days_only(self):
        now = time.mktime((2026, 11, 7, 12, 0, 0, 0, 0, -1))
        d = 86400
        store.crashes_dir().mkdir(parents=True)
        # Files without a readable crash object: the id's time decides.
        ids = self._plant(store.crashes_dir(), now, 10, 8 * d, 29 * d, 30 * d, 45 * d)
        store.thin(now)
        assert crash_files() == sorted(f"{i}.json" for i in ids[:3])

    def test_a_crash_point_lives_thirty_days_from_its_detection(self):
        # The snapshot was saved 31 days before the server died (nothing had
        # changed, or Merlin was stopped): the crash point is kept thirty days
        # from the detection, its old id being only the cheap filter.
        now = time.mktime((2026, 11, 7, 12, 0, 0, 0, 0, -1))
        d = 86400
        entry_id, _ = store.write_crash(
            make("a", saved_at=now - 31 * d), "startup", now
        )
        assert store.thin(now) == []
        assert store.thin(now + 29 * d) == []
        assert crash_files() == [f"{entry_id}.json"]
        assert [p.stem for p in store.thin(now + 30 * d)] == [entry_id]
        assert crash_files() == []

    def test_thinning_an_empty_home_is_fine(self):
        assert store.thin() == []
