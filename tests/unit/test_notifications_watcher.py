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

    def test_agent_window_without_sid_is_ignored(self):
        w = make()
        assert w.observe([win(sid="", state="done", wid="@3")]) == []
        assert w.observe([win(sid="", state="ask", wid="@3")]) == []

    def test_sid_appearing_on_a_done_window_emits_exactly_once(self):
        # The sweep can catch the SessionStart hooks between the state and the
        # sid being stamped. The row counts once, under its stable sid, never
        # a second time when the sid shows up.
        w = make()
        assert w.observe([win(sid="", state="done", wid="@3")]) == []
        (ev,) = w.observe([win(sid="s9", state="done", wid="@3")])
        assert ev.sid == "s9"
        assert w.observe([win(sid="s9", state="done", wid="@3")]) == []

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

    def test_old_epoch_with_ring_overrun_reports_dropped_count(self):
        w = make(ring_size=3)
        for i in range(5):
            w.observe([win(sid=f"s{i}", state="done")])
        events, cursor, dropped = w.events_since("deadbeef:42")
        assert [e.sid for e in events] == ["s2", "s3", "s4"]
        assert dropped == 2
        assert cursor == w.cursor

    def test_cursor_and_events_are_one_snapshot_under_concurrent_publication(self):
        # Contract: whatever interleaving a poll thread sees, the cursor it
        # gets back never runs ahead of the events it received. Both a
        # publication and a read wait on the same lock, so a read either lands
        # entirely before the event (nothing, cursor :0) or entirely after it
        # (the event, cursor :1). Never nothing with cursor :1.
        import threading

        w = make()
        _, c0, _ = w.events_since(None)
        results: list[tuple[list[Event], str, int]] = []
        w._lock.acquire()
        publisher = threading.Thread(target=lambda: w.observe([win(state="done")]))
        reader = threading.Thread(target=lambda: results.append(w.events_since(c0)))
        publisher.start()
        reader.start()
        publisher.join(0.05)
        reader.join(0.05)
        assert publisher.is_alive() and reader.is_alive()  # both parked on the lock
        w._lock.release()
        publisher.join(2)
        reader.join(2)
        (events, cursor, dropped) = results[0]
        assert len(events) == int(cursor.split(":")[1])
        assert dropped == 0

    def test_cursor_ahead_of_seq_returns_nothing(self):
        w = make()
        w.observe([win(state="done")])
        assert w.events_since(f"{w.epoch}:999") == ([], w.cursor, 0)


class TestTask:
    @pytest.mark.asyncio
    async def test_tick_runs_the_sweep_off_the_loop_and_folds_it(self):
        w = make([[win(state="done")]], machine="m")
        assert await w.tick() == 1
        assert w.tmux_available is True
        await w.settle()
        assert len(w.events_since("other:0")[0]) == 1

    @pytest.mark.asyncio
    async def test_tick_swallows_a_raising_sweep(self):
        def boom():
            raise OSError("tmux exploded")

        w = Watcher(boom)
        assert await w.tick() == 0
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

    @pytest.mark.asyncio
    async def test_stop_reraises_its_own_cancellation(self, monkeypatch):
        class Stuck(Watcher):
            async def run(self, stop):
                await asyncio.Event().wait()  # never honours stop

        monkeypatch.setattr(mod, "watcher", Stuck(lambda: []))
        monkeypatch.setattr(mod, "_task", None)
        monkeypatch.setattr(mod, "_stop", None)
        task = mod.start()
        stopper = asyncio.create_task(mod.stop(timeout=10))
        await asyncio.sleep(0.02)
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_stop_cancels_a_task_that_overruns_the_timeout(self, monkeypatch):
        class Stuck(Watcher):
            async def run(self, stop):
                await asyncio.Event().wait()

        monkeypatch.setattr(mod, "watcher", Stuck(lambda: []))
        monkeypatch.setattr(mod, "_task", None)
        monkeypatch.setattr(mod, "_stop", None)
        task = mod.start()
        await mod.stop(timeout=0.05)
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()


class TestBusyTracking:
    """The duration is counted from the sweep that last saw the window enter
    busy, and it is unknown when the watcher never saw that."""

    def clock(self):
        t = {"now": 100.0}

        def read():
            return t["now"]

        return t, read

    def test_busy_then_done_gives_the_duration(self):
        t, clock = self.clock()
        w = make(clock=clock, machine="m")
        w.observe([win(state="idle")])
        t["now"] = 110.0
        w.observe([win(state="busy")])
        t["now"] = 950.0
        (ev,) = w.observe([win(state="done")])
        assert ev.busy_seconds == 840
        assert ev.body == "Finished after 14 min"

    def test_first_seen_done_gives_none(self):
        w = make(machine="m")
        (ev,) = w.observe([win(state="done")])
        assert ev.busy_seconds is None
        assert ev.body == "Finished"

    def test_first_seen_busy_is_an_unknown_start(self):
        # Merlin started after the agent went busy: no duration, not a wrong one.
        t, clock = self.clock()
        w = make(clock=clock)
        w.observe([win(state="busy")])
        t["now"] = 200.0
        (ev,) = w.observe([win(state="done")])
        assert ev.busy_seconds is None

    def test_busy_idle_busy_done_counts_from_the_last_busy(self):
        t, clock = self.clock()
        w = make(clock=clock)
        w.observe([win(state="idle")])
        t["now"] = 110.0
        w.observe([win(state="busy")])
        t["now"] = 500.0
        w.observe([win(state="idle")])
        t["now"] = 600.0
        w.observe([win(state="busy")])
        t["now"] = 640.0
        (ev,) = w.observe([win(state="done")])
        assert ev.busy_seconds == 40

    def test_busy_stays_busy_keeps_the_original_start(self):
        t, clock = self.clock()
        w = make(clock=clock)
        w.observe([win(state="idle")])
        t["now"] = 110.0
        w.observe([win(state="busy")])
        t["now"] = 120.0
        w.observe([win(state="busy")])
        t["now"] = 170.0
        (ev,) = w.observe([win(state="done")])
        assert ev.busy_seconds == 60

    def test_the_span_is_spent_by_the_transition(self):
        # busy -> ask counts the span, ask -> done without a new busy has none.
        t, clock = self.clock()
        w = make(clock=clock)
        w.observe([win(state="idle")])
        t["now"] = 110.0
        w.observe([win(state="busy")])
        t["now"] = 150.0
        (ask,) = w.observe([win(state="ask")])
        assert ask.busy_seconds == 40
        t["now"] = 160.0
        (done,) = w.observe([win(state="done")])
        assert done.busy_seconds is None

    def test_ask_answered_then_done_counts_from_the_answer(self):
        t, clock = self.clock()
        w = make(clock=clock)
        w.observe([win(state="idle")])
        t["now"] = 110.0
        w.observe([win(state="busy")])
        t["now"] = 150.0
        w.observe([win(state="ask")])
        t["now"] = 200.0
        w.observe([win(state="busy")])
        t["now"] = 230.0
        (ev,) = w.observe([win(state="done")])
        assert ev.busy_seconds == 30

    def test_a_none_sweep_keeps_the_start(self):
        t, clock = self.clock()
        w = make(clock=clock)
        w.observe([win(state="idle")])
        t["now"] = 110.0
        w.observe([win(state="busy")])
        t["now"] = 120.0
        assert w.observe(None) == []
        t["now"] = 170.0
        (ev,) = w.observe([win(state="done")])
        assert ev.busy_seconds == 60

    def test_a_window_that_disappears_is_forgotten(self):
        t, clock = self.clock()
        w = make(clock=clock)
        w.observe([win(state="idle")])
        t["now"] = 110.0
        w.observe([win(state="busy")])
        w.observe([])
        assert w._busy_since == {}
        t["now"] = 500.0
        (ev,) = w.observe([win(state="done")])
        assert ev.busy_seconds is None


class TestCaptureAndComposition:
    def test_snippet_is_captured_and_composed_into_the_event(self):
        captured = []

        def capture(window):
            captured.append(window.window_id)
            return "● The report is written.\n\n────\n❯ \n────\n"

        w = make(capture=capture, machine="sandbox")
        (ev,) = w.observe([win(state="done", name="claude", session="proj")])
        assert captured == ["@1"]
        assert ev.snippet == "The report is written."
        assert ev.machine == "sandbox"
        assert ev.title == "claude · proj · sandbox"
        assert ev.body == "Finished: The report is written."
        d = ev.to_dict()
        for key in ("machine", "busy_seconds", "snippet", "title", "body", "target"):
            assert key in d
        assert d["project"] == "proj"

    def test_ask_body(self):
        w = make(capture=lambda _w: "Which one?\n ❯ 1. A\n   2. B\n", machine="m")
        (ev,) = w.observe([win(state="ask")])
        assert ev.body == "Needs an answer: Which one?"

    def test_capture_only_for_windows_that_transitioned(self):
        captured = []

        def capture(window):
            captured.append(window.sid)
            return ""

        w = make(capture=capture)
        w.observe([win(sid="a", state="busy"), win(sid="b", state="done")])
        w.observe([win(sid="a", state="busy"), win(sid="b", state="done")])
        w.observe([win(sid="a", state="done"), win(sid="b", state="done")])
        assert captured == ["b", "a"]

    def test_a_scripted_watcher_captures_nothing_by_default(self):
        w = make(machine="m")
        (ev,) = w.observe([win(state="done")])
        assert ev.snippet == "" and ev.body == "Finished"

    def test_capture_that_raises_still_produces_the_event(self):
        def capture(_w):
            raise RuntimeError("tmux hung")

        w = make(capture=capture, machine="m")
        (ev,) = w.observe([win(state="done")])
        assert ev.snippet == ""
        assert ev.body == "Finished"

    def test_capture_that_returns_none_or_chrome_gives_no_snippet(self):
        w = make(capture=lambda _w: None, machine="m")
        (ev,) = w.observe([win(state="done")])
        assert ev.snippet == ""
        w = make(capture=lambda _w: "────\n❯ \n────\n", machine="m")
        (ev,) = w.observe([win(sid="z", state="done")])
        assert ev.snippet == ""

    def test_machine_missing_leaves_the_title_without_it(self):
        w = make(machine="")
        (ev,) = w.observe([win(state="done", name="", session="s")])
        assert ev.title == "window · s"

    def test_machine_resolves_lazily_when_not_given(self, monkeypatch):
        import merlin_ext

        monkeypatch.setattr(merlin_ext, "resolve_machine_name", lambda: "lazy")
        w = make()
        assert w._machine is None
        (ev,) = w.observe([win(state="done")])
        assert ev.machine == "lazy"

    def test_event_fields_are_additive(self):
        w = make(machine="m")
        (ev,) = w.observe([win(state="done")])
        d = ev.to_dict()
        for key in (
            "seq",
            "sid",
            "session",
            "window_id",
            "window_name",
            "state",
            "project",
            "ts",
            "target",
        ):
            assert key in d


class TestTickCapture:
    """The task path: captures in owned tasks, never on the sweep's clock,
    events published in transition order once their capture completes."""

    @pytest.mark.asyncio
    async def test_tick_captures_off_the_loop_only_for_transitions(self):
        import threading

        loop_thread = threading.get_ident()
        threads = []

        def capture(window):
            threads.append(threading.get_ident())
            return "● Hello from " + window.sid + "\n"

        w = make(
            [
                [win(sid="a", state="busy"), win(sid="b", state="done")],
                [win(sid="a", state="done"), win(sid="b", state="done")],
            ],
            capture=capture,
            machine="m",
        )
        got = []
        w.add_listener(got.append)
        assert await w.tick() == 1
        assert await w.tick() == 1
        await w.settle()
        assert [e.sid for e in got] == ["b", "a"]
        assert got[1].body == "Finished: Hello from a"
        assert len(threads) == 2 and all(t != loop_thread for t in threads)

    @pytest.mark.asyncio
    async def test_tick_with_a_raising_capture_still_emits_and_notifies(self):
        got = []

        def capture(_w):
            raise OSError("no tmux")

        w = make([[win(state="done")]], capture=capture, machine="m")
        w.add_listener(got.append)
        await w.tick()
        await w.settle()
        (ev,) = got
        assert ev.body == "Finished" and ev.snippet == ""

    @pytest.mark.asyncio
    async def test_the_event_is_published_after_the_capture_with_the_snippet(self):
        got = []
        w = make([[win(state="done")]], capture=lambda _w: "● Ready.\n", machine="m")
        w.add_listener(got.append)
        await w.tick()
        await w.settle()
        assert got[0].snippet == "Ready."
        assert w.events_since("other:0")[0] == got

    @pytest.mark.asyncio
    async def test_a_stalled_capture_neither_delays_the_sweep_nor_hides_a_transition(
        self,
    ):
        import threading

        gate = threading.Event()

        def capture(window):
            if window.sid == "slow":
                gate.wait(5)
            return "● from " + window.sid + "\n"

        w = make(
            [
                [win(sid="slow", state="done"), win(sid="fast", state="busy")],
                [win(sid="slow", state="done"), win(sid="fast", state="done")],
                [win(sid="slow", state="done"), win(sid="fast", state="busy")],
                [win(sid="slow", state="done"), win(sid="fast", state="done")],
            ],
            capture=capture,
            machine="m",
        )
        got = []
        w.add_listener(got.append)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            reserved = [await w.tick() for _ in range(4)]
            # The sweeps kept going while the first capture was stalled, and
            # the later transition of the other window was seen both times.
            assert loop.time() - t0 < 1.0
            assert reserved == [1, 1, 0, 1]
            await asyncio.sleep(0.05)
            assert got == []  # transition order: nothing passes the stalled head
            assert w.events_since("other:0") == ([], w.cursor, 0)
        finally:
            gate.set()
        await w.settle(timeout=5)
        assert [(e.sid, e.state, e.seq) for e in got] == [
            ("slow", "done", 1),
            ("fast", "done", 2),
            ("fast", "done", 3),
        ]
        assert [e.snippet for e in got] == ["from slow", "from fast", "from fast"]
        assert got[0].ts <= got[1].ts <= got[2].ts
        assert w.events_since("other:0")[0] == got

    @pytest.mark.asyncio
    async def test_run_keeps_sweeping_while_a_capture_is_slow(self):
        import time as _time

        sweeps = []
        script = [[win(state="done")]]

        def sweep():
            sweeps.append(1)
            return script.pop(0) if script else []

        def capture(_w):
            _time.sleep(0.25)
            return "● Slow.\n"

        w = Watcher(sweep, capture=capture, machine="m", interval=0.01)
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        await asyncio.sleep(0.08)
        assert len(sweeps) >= 3
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        # The stop let the capture in flight publish its event.
        (ev,) = w.events_since("other:0")[0]
        assert ev.snippet == "Slow."

    @pytest.mark.asyncio
    async def test_stop_cancels_a_capture_that_overruns_the_settle(self):
        import threading

        gate = threading.Event()

        def capture(_w):
            gate.wait(5)
            return "● Late.\n"

        w = Watcher(lambda: [win(state="done")], capture=capture, machine="m")
        w.capture_settle = 0.05
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        await asyncio.sleep(0.02)
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=2)
            await asyncio.sleep(0.01)
            assert w._captures == set() and list(w._reserved) == []
            assert w.events_since("other:0") == ([], w.cursor, 0)
        finally:
            gate.set()
