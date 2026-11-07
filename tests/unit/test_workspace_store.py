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


def seen(snapshot, status=None, fillable=()):
    """An Observation: a snapshot read fine (live = its own sessions), or a
    status alone (``snap.NO_SERVER`` / ``snap.FAILED``)."""
    if status is not None:
        return service.Observation(status)
    live = {s["name"]: s["name"] in fillable for s in snapshot["sessions"]}
    return service.Observation(snap.OK, snapshot, live)


def observe(monkeypatch, *items):
    """Make the sweep see ``items`` in turn: snapshots, or status strings."""
    seq = iter(items)

    def next_seen():
        item = next(seq)
        return seen(None, item) if isinstance(item, str) else seen(item)

    monkeypatch.setattr(service, "observe", next_seen)


def sweeps(monkeypatch, state, *items, now=None):
    observe(monkeypatch, *items)
    for _ in items:
        service.sweep_once(state, now)
    return state


T = time.time() - 600  # a recent base time for the detection tests


def served(snapshot, pid, start=100):
    return {**snapshot, "server": {"pid": pid, "start_time": start}}


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
        observe(monkeypatch, *shots)
        state = service.sweep_once(service.SweepState())
        assert state.last["saved_at"] == 1
        service.sweep_once(state)
        assert store.read_snapshot(store.latest_path())["saved_at"] == 1  # unchanged
        service.sweep_once(state)
        assert store.read_snapshot(store.latest_path())["saved_at"] == 3

    @pytest.mark.parametrize(
        "shot",
        [snap.NO_SERVER, snap.FAILED, {"version": 1, "saved_at": 5, "sessions": []}],
    )
    def test_never_overwrites_with_no_server_or_empty(self, monkeypatch, shot):
        store.write_snapshot(store.latest_path(), make("a"))
        sweeps(monkeypatch, service.SweepState(last=make("a")), shot)
        assert store.read_snapshot(store.latest_path()) == make("a")

    @pytest.mark.asyncio
    async def test_run_freezes_before_first_write(self, monkeypatch):
        # Right after a reboot: the latest snapshot holds sessions, tmux has a
        # fresh default session only. The offer must survive the first sweep.
        import asyncio

        store.write_snapshot(store.latest_path(), make("main", "portal"))
        live(monkeypatch, ["merlin-dev"])
        monkeypatch.setattr(
            service, "observe", lambda: seen(make("merlin-dev", saved_at=9))
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
            service, "observe", lambda: seen(make("merlin-dev", saved_at=9))
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
            service, "observe", lambda: seen(make("merlin-dev", saved_at=9))
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
        sweeps(monkeypatch, service.SweepState(), *shots)  # 2nd unchanged: no copy
        ids = [n[: -len(".json")] for n in history_files()]
        assert len(ids) == 2
        assert [store.read_snapshot(store.entry_path(i))["saved_at"] for i in ids] == [
            t,
            t + 2,
        ]

    def test_history_failure_never_blocks_the_sweep(self, monkeypatch):
        monkeypatch.setattr(
            store, "write_history", lambda s: (_ for _ in ()).throw(OSError("full"))
        )
        state = sweeps(monkeypatch, service.SweepState(), make("a", saved_at=1))
        assert state.last["saved_at"] == 1  # the snapshot advanced all the same
        assert store.read_snapshot(store.latest_path()) == make("a", saved_at=1)


class TestDetection:
    """The sweep's death detection, with tmux replaced by observations. Times
    are recent: the thinning after each write would remove 1970 entries."""

    def _disk(self, snapshot):
        store.write_snapshot(store.latest_path(), snapshot)
        return service.SweepState(last=snapshot)

    def test_server_changed_freezes_before_the_write(self, monkeypatch):
        old = served(make("a", "b", saved_at=T + 10), pid=1)
        new = served(make("merlin-dev", saved_at=T + 20), pid=2)
        state = sweeps(monkeypatch, self._disk(old), new, now=T + 25.0)
        pending = offer()
        assert without_crash(pending) == old
        assert pending["crash"] == {"reason": "server-changed", "detected_at": T + 25.0}
        assert (
            store.read_snapshot(store.entry_path(pending["id"]))["crash"]
            == (pending["crash"])
        )
        assert store.read_snapshot(store.latest_path()) == new  # written after
        assert state.last == new

    def test_server_gone_needs_two_sweeps_and_a_failed_read_counts_for_nothing(
        self, monkeypatch
    ):
        old = served(make("a", saved_at=T + 10), pid=1)
        state = sweeps(monkeypatch, self._disk(old), snap.NO_SERVER)
        assert offer() is None and crash_files() == []
        sweeps(monkeypatch, state, snap.FAILED)  # neither a second miss nor a reset
        assert offer() is None and state.no_server == 1
        sweeps(monkeypatch, state, snap.NO_SERVER, now=T + 30.0)
        assert offer()["crash"] == {"reason": "server-gone", "detected_at": T + 30.0}
        assert store.read_snapshot(store.latest_path()) == old  # untouched

    def test_a_server_seen_again_resets_the_count(self, monkeypatch):
        old = served(make("a", saved_at=T + 10), pid=1)
        state = sweeps(
            monkeypatch, self._disk(old), snap.NO_SERVER, old, snap.NO_SERVER
        )
        assert state.no_server == 1 and offer() is None

    def test_old_snapshot_without_server_never_changes_server(self, monkeypatch):
        old = make("a", saved_at=T + 10)  # written before the field existed
        new = served(make("merlin-dev", saved_at=T + 20), pid=2)
        sweeps(monkeypatch, self._disk(old), new)
        assert offer() is None and crash_files() == []
        assert store.read_snapshot(store.latest_path()) == new
        # The other way round too: a new server whose identity is unknown.
        sweeps(monkeypatch, self._disk(served(old, pid=1)), make("x", saved_at=T + 30))
        assert offer() is None

    def test_a_dead_server_is_frozen_once(self, monkeypatch):
        old = served(make("a", saved_at=T + 10), pid=1)
        # The new server only holds the jobs session: nothing is written, the
        # same rule fires again on every sweep, and freezes nothing more.
        empty = served({**make(), "saved_at": T + 20}, pid=2)
        state = sweeps(monkeypatch, self._disk(old), empty, empty, empty)
        assert len(crash_files()) == 1
        # A fresh process (Merlin restarted) finds it frozen on disk.
        sweeps(
            monkeypatch, service.SweepState(last=old), snap.NO_SERVER, snap.NO_SERVER
        )
        assert len(crash_files()) == 1
        assert state.handled == {("server", 1, 100)}

    def test_startup_freeze_then_the_sweep_does_not_freeze_again(self, monkeypatch):
        # After a reboot: the startup freeze made the offer; the sweeps then
        # find no server twice, and must not freeze a second crash point.
        old = served(make("a", saved_at=T + 10), pid=1)
        store.write_snapshot(store.latest_path(), old)
        live(monkeypatch, None)
        assert service.freeze_pending(now=T + 11.0) is True
        state = service.SweepState(last=store.read_snapshot(store.latest_path()))
        sweeps(monkeypatch, state, snap.NO_SERVER, snap.NO_SERVER, snap.NO_SERVER)
        assert len(crash_files()) == 1
        assert offer()["crash"]["reason"] == "startup"

    def test_nothing_missing_freezes_nothing(self, monkeypatch):
        # The user rebuilt everything by hand before the sweep noticed.
        old = served(make("a", "b", saved_at=T + 10), pid=1)
        new = served(make("a", "b", saved_at=T + 20), pid=2)
        state = sweeps(monkeypatch, self._disk(old), new)
        assert offer() is None and crash_files() == []
        assert store.read_snapshot(store.latest_path()) == new
        assert ("server", 1, 100) in state.handled

    def test_a_second_crash_replaces_the_offer_and_keeps_both_points(self, monkeypatch):
        first = served(make("a", saved_at=T + 10), pid=1)
        second = served(make("b", saved_at=T + 20), pid=2)
        third = served(make("merlin-dev", saved_at=T + 30), pid=3)
        state = sweeps(monkeypatch, self._disk(first), second, now=T + 21.0)
        assert without_crash(offer()) == first
        sweeps(monkeypatch, state, third, now=T + 31.0)
        pending = offer()
        assert without_crash(pending) == second
        assert pending["crash"]["detected_at"] == T + 31.0
        assert len(crash_files()) == 2
        ids = sorted(p.stem for p in store.crashes_dir().glob("*.json"))
        assert [
            store.read_snapshot(store.entry_path(i))["sessions"][0]["name"] for i in ids
        ] == ["a", "b"]

    def _fail_once(self, monkeypatch, target):
        """Make the first write to ``target`` (a path) fail."""
        real = store.write_snapshot
        tripped = []

        def flaky(path, data):
            if path == target and not tripped:
                tripped.append(1)
                raise OSError("no space left on device")
            real(path, data)

        monkeypatch.setattr(store, "write_snapshot", flaky)
        return tripped

    @pytest.mark.parametrize("older", [False, True])
    def test_a_failed_offer_write_is_redone_by_a_fresh_process(
        self, monkeypatch, older
    ):
        # The offer's write fails: nothing is on disk for this crash (the
        # crash point comes after the offer), latest.json is untouched, and
        # the next sweep, from a fresh process too, freezes it whole.
        first = served(make("a", saved_at=T + 10), pid=1)
        second = served(make("merlin-dev", saved_at=T + 20), pid=2)
        state = self._disk(first)
        if older:
            service.freeze(served(make("z", saved_at=T + 1), pid=9), "startup", T + 2)
        before = offer()
        self._fail_once(monkeypatch, store.pending_path())
        observe(monkeypatch, second, second)
        with pytest.raises(OSError):
            service.sweep_once(state, now=T + 21.0)
        assert store.read_snapshot(store.latest_path()) == first
        assert offer() == before  # the older offer, or none, untouched
        assert len(crash_files()) == (1 if older else 0)
        service.sweep_once(service.SweepState(last=first), now=T + 36.0)
        pending = offer()
        assert without_crash(pending) == first
        assert pending["crash"]["detected_at"] == T + 36.0
        assert (
            store.read_snapshot(store.entry_path(pending["id"]))["crash"]
            == (pending["crash"])
        )
        assert len(crash_files()) == (2 if older else 1)
        assert store.read_snapshot(store.latest_path()) == second

    def test_a_failed_crash_write_is_redone_by_a_fresh_process(self, monkeypatch):
        # The offer is published, the crash point's write fails: the offer
        # stands (a full copy), latest.json is untouched, the next sweep
        # publishes again and keeps the crash point. One crash point in all.
        first = served(make("a", saved_at=T + 10), pid=1)
        second = served(make("merlin-dev", saved_at=T + 20), pid=2)
        state = self._disk(first)
        real = store.write_crash
        tripped = []

        def flaky(entry_id, point):
            if not tripped:
                tripped.append(1)
                raise OSError("no space left on device")
            real(entry_id, point)

        monkeypatch.setattr(store, "write_crash", flaky)
        observe(monkeypatch, second, second)
        with pytest.raises(OSError):
            service.sweep_once(state, now=T + 21.0)
        assert without_crash(offer()) == first
        assert crash_files() == []
        assert store.read_snapshot(store.latest_path()) == first
        service.sweep_once(service.SweepState(last=first), now=T + 36.0)
        pending = offer()
        assert pending["crash"]["detected_at"] == T + 36.0
        assert crash_files() == [pending["id"] + ".json"]
        assert store.read_snapshot(store.latest_path()) == second

    def test_a_failed_freeze_is_retried_next_sweep(self, monkeypatch):
        old = served(make("a", saved_at=T + 10), pid=1)
        new = served(make("merlin-dev", saved_at=T + 20), pid=2)
        state = self._disk(old)
        calls = []

        def flaky(snapshot, reason, now=None):
            calls.append(reason)
            if len(calls) == 1:
                raise OSError("no space left on device")
            return service.__dict__["_real_freeze_fn"](snapshot, reason, now)

        service.__dict__["_real_freeze_fn"] = service.freeze
        monkeypatch.setattr(service, "freeze", flaky)
        observe(monkeypatch, new, new)
        with pytest.raises(OSError):
            service.sweep_once(state)
        assert store.read_snapshot(store.latest_path()) == old  # not written over
        service.sweep_once(state)
        assert without_crash(offer()) == old
        assert store.read_snapshot(store.latest_path()) == new
        del service.__dict__["_real_freeze_fn"]


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


class TestNewerOfferSurvives:
    """The sweep publishes a newer offer while the banner's request works on
    the older one: the removal names the offer it examined."""

    def _publish_b(self):
        service.freeze(
            served(make("b", saved_at=T + 20), pid=2), "server-changed", T + 21
        )

    def test_pending_info_dropping_a_stale_offer_keeps_the_newer_one(self, monkeypatch):
        from workspace import restore as rs

        service.freeze(served(make("a", saved_at=T + 10), pid=1), "server-gone", T + 11)
        monkeypatch.setattr(rs, "live_sessions", lambda: {"a": False})

        def rebuilt_then_b(snapshot, live, job_runs=()):
            self._publish_b()  # between the read and the removal
            return []

        monkeypatch.setattr(rs, "missing_sessions", rebuilt_then_b)
        assert service.pending_info() == {"pending": False}
        assert offer()["sessions"][0]["name"] == "b"

    def test_a_restore_completing_keeps_the_newer_offer(self, monkeypatch):
        from workspace import restore as rs

        service.freeze(served(make("a", saved_at=T + 10), pid=1), "server-gone", T + 11)

        def restore_during_which_b_dies(snapshot):
            self._publish_b()
            return {"restored": ["a"], "skipped": [], "failed": [], "agents": 0}

        monkeypatch.setattr(rs, "restore", restore_during_which_b_dies)
        assert service.restore_pending()["restored"] == ["a"]
        assert offer()["sessions"][0]["name"] == "b"

    def test_dismiss_names_the_offer_the_user_saw(self):
        service.freeze(served(make("a", saved_at=T + 10), pid=1), "server-gone", T + 11)
        a = offer()["id"]
        self._publish_b()
        service.dismiss_pending(a)
        assert offer()["sessions"][0]["name"] == "b"
        service.dismiss_pending()  # unnamed: whatever is there
        assert offer() is None

    def test_delete_pending_compares_the_id(self):
        assert store.delete_pending("x") is False  # nothing there
        store.write_pending(make("legacy"))  # an offer from before ids
        assert store.delete_pending("x") is False
        assert store.delete_pending(None) is True
        store.write_pending({**make("a"), "id": "one"})
        assert store.delete_pending("two") is False
        assert store.delete_pending("one") is True
        assert offer() is None


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
        third = store.new_id(shot["saved_at"])
        store.write_crash(third, store.frozen(shot, "server-gone", 1001.0))
        assert second == first + "-2"
        assert third == first + "-3"
        assert store.entry_path(first) == store.history_dir() / f"{first}.json"
        assert store.entry_path(third) == store.crashes_dir() / f"{third}.json"
        assert store.entry_path("nope") is None
        assert store.entry_path("../latest") is None

    def test_a_crash_point_carries_its_crash_object(self):
        shot = make("a", saved_at=1000.0)
        frozen = store.frozen(shot, "server-changed", 1234.5)
        entry_id = store.new_id(shot["saved_at"])
        store.write_crash(entry_id, frozen)
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
        old = make("a", saved_at=now - 31 * d)
        entry_id = store.new_id(old["saved_at"])
        store.write_crash(entry_id, store.frozen(old, "startup", now))
        assert store.thin(now) == []
        assert store.thin(now + 29 * d) == []
        assert crash_files() == [f"{entry_id}.json"]
        assert [p.stem for p in store.thin(now + 30 * d)] == [entry_id]
        assert crash_files() == []

    def test_thinning_an_empty_home_is_fine(self):
        assert store.thin() == []
