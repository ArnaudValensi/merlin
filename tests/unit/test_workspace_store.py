"""Workspace files and the startup freeze (workspace/store.py, service.py)."""

import json

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


class TestFreeze:
    def test_missing_sessions_freeze_an_offer(self, monkeypatch):
        store.write_snapshot(store.latest_path(), make("a", "b"))
        live(monkeypatch, None)  # no tmux server: after a reboot
        assert service.freeze_pending() is True
        assert store.read_snapshot(store.pending_path()) == make("a", "b")

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
        assert store.read_snapshot(store.pending_path()) == rich

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
        assert store.read_snapshot(store.pending_path()) == make("old")

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
        assert store.read_snapshot(store.pending_path()) == make("main", "portal")
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
        assert store.read_snapshot(store.pending_path()) == make("main")
        del service.__dict__["_real_freeze"]


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
