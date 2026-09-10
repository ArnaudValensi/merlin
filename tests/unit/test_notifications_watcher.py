"""Tests for the attention watcher (notifications/watcher.py). Fake sweeps in,
events out: no tmux, no event loop except where the task itself is under test."""

import asyncio

import pytest

from board.sweep import Window
import notifications.watcher as mod
from notifications.watcher import Event, Watcher


def win(
    sid="s1", state="busy", wid="@1", session="alpha", name="claude", cwd="/h/u/proj"
):
    return Window(
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


def make(sweeps=None, **kw):
    """A watcher fed by a scripted list of sweeps (used by the task tests)."""
    script = list(sweeps or [])

    def sweep():
        return script.pop(0) if script else []

    return Watcher(sweep, **kw)


class TestTransitions:
    def test_first_sight_in_done_is_a_transition(self):
        w = make()
        (ev,) = w.observe([win(state="done")])
        assert ev.state == "done"
        assert ev.sid == "s1"

    def test_first_sight_in_ask_is_a_transition(self):
        w = make()
        (ev,) = w.observe([win(state="ask")])
        assert ev.state == "ask"

    def test_first_sight_in_busy_or_idle_is_nothing(self):
        w = make()
        assert w.observe([win(state="busy"), win(sid="s2", state="idle")]) == []

    def test_busy_to_done_is_one_event(self):
        w = make()
        assert w.observe([win(state="busy")]) == []
        assert len(w.observe([win(state="done")])) == 1

    def test_done_observed_twice_is_not_a_transition(self):
        w = make()
        assert len(w.observe([win(state="done")])) == 1
        assert w.observe([win(state="done")]) == []
        assert w.observe([win(state="done")]) == []

    def test_ask_to_busy_to_ask_fires_twice(self):
        w = make()
        assert len(w.observe([win(state="ask")])) == 1
        assert w.observe([win(state="busy")]) == []
        assert len(w.observe([win(state="ask")])) == 1

    def test_done_to_ask_is_a_transition(self):
        w = make()
        w.observe([win(state="done")])
        (ev,) = w.observe([win(state="ask")])
        assert ev.state == "ask"

    def test_done_to_idle_produces_nothing(self):
        w = make()
        w.observe([win(state="done")])
        assert w.observe([win(state="idle")]) == []
        assert w.observe([win(state="")]) == []

    def test_window_renamed_keeps_identity_by_sid(self):
        w = make()
        w.observe([win(state="done", name="claude", wid="@1")])
        # Same sid, new window name and even a new window id: still no event.
        assert w.observe([win(state="done", name="review", wid="@7")]) == []

    def test_plain_windows_are_ignored(self):
        w = make()
        assert w.observe([win(sid="", state="", name="zsh")]) == []

    def test_agent_window_without_sid_is_keyed_by_window_id(self):
        w = make()
        (ev,) = w.observe([win(sid="", state="done", wid="@3")])
        assert ev.sid == "win:@3"
        assert w.observe([win(sid="", state="done", wid="@3")]) == []

    def test_window_gone_then_back_in_done_is_a_new_transition(self):
        w = make()
        w.observe([win(state="done")])
        assert w.observe([]) == []
        assert len(w.observe([win(state="done")])) == 1

    def test_several_windows_each_get_their_own_event(self):
        w = make()
        evs = w.observe(
            [
                win(sid="a", state="done"),
                win(sid="b", state="ask"),
                win(sid="c", state="busy"),
            ]
        )
        assert sorted(e.sid for e in evs) == ["a", "b"]


class TestTmuxGone:
    def test_none_sweep_marks_tmux_unavailable_and_emits_nothing(self):
        w = make()
        assert w.tmux_available is None
        assert w.observe(None) == []
        assert w.tmux_available is False
        assert w.swept_once is True

    def test_tmux_back_with_same_windows_does_not_refire(self):
        w = make()
        assert len(w.observe([win(state="done")])) == 1
        assert w.observe(None) == []
        assert w.observe([win(state="done")]) == []
        assert w.tmux_available is True

    def test_tmux_back_with_a_new_done_window_fires(self):
        w = make()
        w.observe([win(state="done")])
        w.observe(None)
        evs = w.observe([win(state="done"), win(sid="s2", state="done")])
        assert [e.sid for e in evs] == ["s2"]


class TestEventShape:
    def test_event_fields(self):
        w = make(clock=lambda: 1_700_000_000.0)
        (ev,) = w.observe([win(state="done", cwd="/home/u/merlin-saas/", name="build")])
        assert ev.session == "alpha"
        assert ev.window_id == "@1"
        assert ev.window_name == "build"
        assert ev.project == "merlin-saas"
        assert ev.ts == "2023-11-14T22:13:20+00:00"
        assert ev.target == "alpha:@1"
        d = ev.to_dict()
        assert d["target"] == "alpha:@1"
        assert d["seq"] == 1

    def test_seq_increases(self):
        w = make()
        (a,) = w.observe([win(sid="a", state="done")])
        (b,) = w.observe([win(sid="b", state="done")])
        assert (a.seq, b.seq) == (1, 2)

    def test_listeners_get_each_event_and_a_failing_one_is_isolated(self):
        w = make()
        got: list[Event] = []

        def bad(_ev):
            raise RuntimeError("boom")

        w.add_listener(bad)
        w.add_listener(got.append)
        (ev,) = w.observe([win(state="done")])
        assert got == [ev]


class TestCursor:
    def test_no_cursor_gets_nothing_and_the_current_position(self):
        w = make()
        w.observe([win(state="done")])
        events, cursor, dropped = w.events_since(None)
        assert events == []
        assert cursor == w.cursor
        assert dropped == 0
        assert w.events_since("") == ([], w.cursor, 0)

    def test_events_after_cursor(self):
        w = make()
        _, c0, _ = w.events_since(None)
        w.observe([win(sid="a", state="done")])
        w.observe([win(sid="a", state="done"), win(sid="b", state="done")])
        events, c1, dropped = w.events_since(c0)
        assert [e.sid for e in events] == ["a", "b"]
        assert dropped == 0
        assert w.events_since(c1) == ([], c1, 0)

    def test_cursor_from_another_process_gets_everything_of_this_one(self):
        w = make()
        w.observe([win(sid="a", state="done")])
        events, cursor, dropped = w.events_since("deadbeef:42")
        assert [e.sid for e in events] == ["a"]
        assert cursor == w.cursor
        assert dropped == 0

    def test_garbage_cursor_is_treated_like_another_process(self):
        w = make()
        w.observe([win(sid="a", state="done")])
        events, _, _ = w.events_since("not a cursor")
        assert len(events) == 1
        events, _, _ = w.events_since(w.epoch + ":x")
        assert len(events) == 1

    def test_ring_overrun_reports_dropped_count(self):
        w = make(ring_size=3)
        _, c0, _ = w.events_since(None)
        for i in range(5):
            w.observe([win(sid=f"s{i}", state="done")])
        events, _, dropped = w.events_since(c0)
        assert [e.sid for e in events] == ["s2", "s3", "s4"]
        assert dropped == 2

    def test_cursor_ahead_of_seq_returns_nothing(self):
        w = make()
        w.observe([win(state="done")])
        assert w.events_since(f"{w.epoch}:999") == ([], w.cursor, 0)


class TestTask:
    @pytest.mark.asyncio
    async def test_tick_runs_the_sweep_off_the_loop_and_folds_it(self):
        w = make([[win(state="done")]])
        evs = await w.tick()
        assert len(evs) == 1
        assert w.tmux_available is True

    @pytest.mark.asyncio
    async def test_tick_swallows_a_raising_sweep(self):
        def boom():
            raise OSError("tmux exploded")

        w = Watcher(boom)
        assert await w.tick() == []
        assert w.tmux_available is False

    @pytest.mark.asyncio
    async def test_run_stops_cleanly(self):
        w = make([[win(state="done")], [win(state="done")]], interval=0.01)
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        assert w.swept_once
        assert len(w.events_since("other:0")[0]) == 1

    @pytest.mark.asyncio
    async def test_module_start_and_stop(self, monkeypatch):
        fake = make([[win(state="done")]], interval=0.01)
        monkeypatch.setattr(mod, "watcher", fake)
        monkeypatch.setattr(mod, "_task", None)
        monkeypatch.setattr(mod, "_stop", None)
        t1 = mod.start()
        assert mod.start() is t1  # idempotent
        await asyncio.sleep(0.03)
        await mod.stop()
        assert t1.done()
        await mod.stop()  # a second stop is a no-op
