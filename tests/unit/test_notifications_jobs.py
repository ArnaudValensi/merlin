"""Job runs through the attention watcher (notifications/watcher.py): the
report mode applied to a run's own finish, the finishing race, a human's
later turn. Fake sweeps in, events out: no tmux."""

from __future__ import annotations

import pytest

from board.sweep import Window
from notifications.content import compose_body
from notifications.watcher import Watcher


def win(
    sid="j1",
    state="busy",
    report="always",
    exit_code="",
    finished="",
    job_id="digest",
    name="digest",
):
    return Window(
        sid=sid,
        state=state,
        cwd="/h/u/proj",
        parent="",
        relation="",
        session="merlin-jobs",
        window_id="@7",
        index=1,
        active=False,
        activity=0,
        name=name,
        job_id=job_id,
        job_report=report,
        job_exit=exit_code,
        job_finished=finished,
    )


def watcher(clock=None):
    """A watcher with a clock that advances 10 s per sweep, which has seen
    the window idle once (so a busy span is known)."""
    times = iter(range(100, 10_000, 10)) if clock is None else clock
    w = Watcher(lambda: [], machine="box", clock=lambda: next(times))
    return w


def seen_idle(w: Watcher, **over) -> Watcher:
    assert w.observe([win(state="idle", **over)]) == []
    return w


@pytest.mark.parametrize(
    ("report", "exit_code", "notified"),
    [
        ("always", "0", True),
        ("always", "3", True),
        ("silent", "0", False),
        ("silent", "3", True),
        ("off", "0", False),
        ("off", "3", False),
    ],
)
def test_the_report_mode_table_on_a_finished_run(report, exit_code, notified):
    """A command run: run.sh stamps the exit and done together."""
    w = seen_idle(watcher(), report=report)
    assert w.observe([win(state="busy", report=report)]) == []
    events = w.observe([win(state="done", report=report, exit_code=exit_code)])
    assert bool(events) is notified
    if notified:
        (ev,) = events
        assert ev.job_id == "digest"
        assert ev.exit_code == int(exit_code)
        if exit_code == "0":
            assert ev.body.startswith("Finished after 10 s")
        else:
            assert ev.body.startswith("Failed (exit 3) after 10 s")


@pytest.mark.parametrize(
    ("report", "notified"), [("always", True), ("silent", True), ("off", False)]
)
def test_the_report_mode_table_on_a_blocked_run(report, notified):
    w = seen_idle(watcher(), report=report)
    assert w.observe([win(state="busy", report=report)]) == []
    events = w.observe([win(state="ask", report=report)])
    assert bool(events) is notified
    if notified:
        (ev,) = events
        assert ev.body.startswith("Needs an answer")
        assert ev.job_id == "digest" and ev.exit_code is None


def test_a_silenced_finish_reaches_no_channel():
    """No event in the ring, none to the listeners."""
    w = seen_idle(watcher(), report="silent")
    got: list = []
    w.add_listener(got.append)
    w.observe([win(state="busy", report="silent")])
    assert w.observe([win(state="done", report="silent", exit_code="0")]) == []
    assert got == []
    assert w.events_since(f"{w.epoch}:0")[0] == []


class TestTheFinishingRace:
    """An agent's Stop stamps done before the supervisor stamps the exit."""

    def test_a_done_without_an_exit_waits_for_it(self):
        w = seen_idle(watcher(), report="silent")
        w.observe([win(state="busy", report="silent")])
        # The done, seen first without the exit: nothing yet, and not a
        # plain done that would slip through silent.
        assert w.observe([win(state="done", report="silent")]) == []
        # The supervisor stamped exit 0: silent keeps it quiet.
        assert w.observe([win(state="done", report="silent", exit_code="0")]) == []
        # Nothing fires later for that finish either.
        assert (
            w.observe([win(state="done", report="silent", exit_code="0", finished="1")])
            == []
        )

    def test_a_deferred_failure_notifies_once_the_exit_is_known(self):
        w = seen_idle(watcher(), report="silent")
        w.observe([win(state="busy", report="silent")])
        assert w.observe([win(state="done", report="silent")]) == []
        (ev,) = w.observe([win(state="done", report="silent", exit_code="2")])
        # The busy span is the one of the sweep that saw the done: the run
        # ended then, the exit only became known later.
        assert ev.exit_code == 2 and ev.body.startswith("Failed (exit 2) after 10 s")
        assert (
            w.observe([win(state="done", report="silent", exit_code="2", finished="1")])
            == []
        )

    def test_a_deferred_finish_under_always_notifies_once(self):
        w = seen_idle(watcher())
        w.observe([win(state="busy")])
        assert w.observe([win(state="done")]) == []
        (ev,) = w.observe([win(state="done", exit_code="0")])
        assert ev.job_id == "digest" and ev.exit_code == 0
        assert w.observe([win(state="done", exit_code="0", finished="1")]) == []

    def test_a_done_that_never_gets_an_exit_is_decided_after_the_wait(self):
        from notifications import watcher as mod

        w = seen_idle(watcher())
        w.observe([win(state="busy")])
        assert w.observe([win(state="done")]) == []
        for _ in range(mod.DEFER_SWEEPS - 1):
            assert w.observe([win(state="done")]) == []
        (ev,) = w.observe([win(state="done")])
        assert ev.job_id == "digest" and ev.exit_code is None
        assert ev.body.startswith("Finished after")

    def test_a_deferred_window_that_vanishes_is_forgotten(self):
        w = seen_idle(watcher())
        w.observe([win(state="busy")])
        assert w.observe([win(state="done")]) == []
        assert w.observe([]) == []
        assert w.observe([]) == []


class TestAFinishSeenFromTheRunFields:
    """The supervisor can stamp the exit and the finish before the agent's
    done reaches the window, or the run can end busy-then-idle with no done
    at all: the run fields confirm the finish, decided once by the mode."""

    @pytest.mark.parametrize(
        ("report", "exit_code", "notified"),
        [
            ("always", "0", True),
            ("always", "3", True),
            ("silent", "0", False),
            ("silent", "3", True),
            ("off", "0", False),
            ("off", "3", False),
        ],
    )
    def test_a_finish_stamped_while_busy_is_decided_by_the_mode(
        self, report, exit_code, notified
    ):
        """run.sh publishes the exit before it stamps done: a sweep between
        sees busy with the exit and the finish. The later done is the run's
        own, not a human's turn."""
        w = seen_idle(watcher(), report=report)
        got: list = []
        w.add_listener(got.append)
        assert w.observe([win(state="busy", report=report)]) == []
        events = w.observe(
            [win(state="busy", report=report, exit_code=exit_code, finished="1")]
        )
        assert bool(events) is notified
        if notified:
            (ev,) = events
            assert ev.state == "done" and ev.job_id == "digest"
            assert ev.exit_code == int(exit_code)
            assert ev.busy_seconds == 10
        # The agent's done lands: nothing more, whatever the mode.
        assert (
            w.observe(
                [win(state="done", report=report, exit_code=exit_code, finished="1")]
            )
            == []
        )
        assert len(got) == (1 if notified else 0)
        assert len(w.events_since(f"{w.epoch}:0")[0]) == len(got)

    @pytest.mark.parametrize(
        ("report", "exit_code", "notified"),
        [("always", "0", True), ("silent", "0", False), ("silent", "3", True)],
    )
    def test_a_busy_then_idle_finish_notifies(self, report, exit_code, notified):
        """The supervisor takes busy followed by idle as a finish when the
        done was missed, and stamps the exit while the window is idle."""
        w = seen_idle(watcher(), report=report)
        assert w.observe([win(state="busy", report=report)]) == []
        assert w.observe([win(state="idle", report=report)]) == []
        events = w.observe(
            [win(state="idle", report=report, exit_code=exit_code, finished="1")]
        )
        assert bool(events) is notified
        if notified:
            (ev,) = events
            assert ev.state == "done" and ev.exit_code == int(exit_code)
            assert ev.body.startswith(
                "Finished after 20 s"
                if exit_code == "0"
                else "Failed (exit 3) after 20 s"
            )
        # A done observed afterwards is the same finish: no second event.
        assert (
            w.observe(
                [win(state="done", report=report, exit_code=exit_code, finished="1")]
            )
            == []
        )
        assert (
            w.observe(
                [win(state="idle", report=report, exit_code=exit_code, finished="1")]
            )
            == []
        )

    def test_a_busy_first_seen_with_the_finish_is_the_runs_own(self):
        """A short run: the sweep sees the busy only together with the exit
        the supervisor stamped over it. No human turn starts there."""
        w = seen_idle(watcher())
        (finish,) = w.observe([win(state="busy", exit_code="0", finished="1")])
        assert finish.state == "done" and finish.job_id == "digest"
        assert w.observe([win(state="done", exit_code="0", finished="1")]) == []

    def test_an_ordinary_idle_transition_stays_silent(self):
        w = seen_idle(watcher())
        w.observe([win(state="busy")])
        assert w.observe([win(state="idle")]) == []
        assert w.observe([win(state="idle")]) == []

    def test_a_finished_window_first_seen_idle_is_left_alone(self):
        """Merlin restarted long after the run: the previous process decided
        that finish. The window is the human's."""
        w = watcher()
        assert w.observe([win(state="idle", exit_code="0", finished="1")]) == []
        assert w.observe([win(state="busy", exit_code="0", finished="1")]) == []
        (ev,) = w.observe([win(state="done", exit_code="0", finished="1")])
        assert ev.job_id == "" and ev.exit_code is None


class TestAStateChangeDuringTheWait:
    """The state moves on while a done waits for its exit: the finish keeps
    its identity (a done, the span of the sweep that saw it) and the new
    transition is the human's, told once."""

    def test_idle_with_the_exit_tells_the_finish_as_a_done(self):
        w = seen_idle(watcher())
        w.observe([win(state="busy")])
        assert w.observe([win(state="done")]) == []
        (ev,) = w.observe([win(state="idle", exit_code="0", finished="1")])
        assert ev.state == "done" and ev.exit_code == 0 and ev.busy_seconds == 10
        assert w.observe([win(state="idle", exit_code="0", finished="1")]) == []

    def test_ask_with_the_exit_is_one_finish_and_one_question(self):
        w = seen_idle(watcher())
        got: list = []
        w.add_listener(got.append)
        w.observe([win(state="busy")])
        assert w.observe([win(state="done")]) == []
        finish, ask = w.observe([win(state="ask", exit_code="0", finished="1")])
        assert finish.state == "done" and finish.exit_code == 0
        assert finish.job_id == "digest" and finish.body.startswith("Finished")
        assert ask.state == "ask" and ask.exit_code is None and ask.job_id == ""
        assert ask.body.startswith("Needs an answer")
        assert len(got) == 2
        assert w.observe([win(state="ask", exit_code="0", finished="1")]) == []

    def test_busy_with_the_exit_starts_the_humans_turn(self):
        w = seen_idle(watcher())
        w.observe([win(state="busy")])
        assert w.observe([win(state="done")]) == []
        (finish,) = w.observe([win(state="busy", exit_code="0", finished="1")])
        assert finish.state == "done" and finish.job_id == "digest"
        (ev,) = w.observe([win(state="done", exit_code="0", finished="1")])
        assert ev.job_id == "" and ev.exit_code is None
        # The fake clock also ticks for the finish event's timestamp.
        assert ev.body.startswith("Finished after 20 s")

    def test_a_question_while_waiting_is_told_once(self):
        """done (no exit) then ask: the question comes after the run's done,
        so it is the human's, told whatever the mode; the exit then decides
        the finish, and the ask is not told again."""
        w = seen_idle(watcher(), report="off")
        w.observe([win(state="busy", report="off")])
        assert w.observe([win(state="done", report="off")]) == []
        (ask,) = w.observe([win(state="ask", report="off")])
        assert ask.state == "ask" and ask.job_id == ""
        assert (
            w.observe([win(state="ask", report="off", exit_code="124", finished="1")])
            == []
        )
        assert (
            w.observe([win(state="ask", report="off", exit_code="124", finished="1")])
            == []
        )
        w = seen_idle(watcher(), report="silent")
        w.observe([win(state="busy", report="silent")])
        assert w.observe([win(state="done", report="silent")]) == []
        (ask,) = w.observe([win(state="ask", report="silent")])
        assert ask.state == "ask" and ask.job_id == ""
        (finish,) = w.observe(
            [win(state="ask", report="silent", exit_code="124", finished="1")]
        )
        assert finish.state == "done" and finish.exit_code == 124
        assert (
            w.observe(
                [win(state="ask", report="silent", exit_code="124", finished="1")]
            )
            == []
        )


class TestANewRunSeenAlreadyFinished:
    """A run that began and ended between two sweeps of a running watcher:
    its first sight carries the finish fields, idle (busy then idle) or
    busy (the exit stamped before the done). Not a restart replay: the
    finish is decided by the mode, and the trailing done is nothing."""

    CELLS = [
        ("always", "0", True),
        ("always", "3", True),
        ("silent", "0", False),
        ("silent", "3", True),
        ("off", "0", False),
        ("off", "3", False),
    ]

    @pytest.mark.parametrize("first", ["idle", "busy"])
    @pytest.mark.parametrize(("report", "exit_code", "notified"), CELLS)
    def test_decided_by_the_mode_at_first_sight(
        self, first, report, exit_code, notified
    ):
        w = watcher()
        assert w.observe([]) == []
        got: list = []
        w.add_listener(got.append)
        events = w.observe(
            [win(state=first, report=report, exit_code=exit_code, finished="1")]
        )
        assert bool(events) is notified
        if notified:
            (ev,) = events
            assert ev.state == "done" and ev.job_id == "digest"
            assert ev.exit_code == int(exit_code)
            assert ev.body.startswith(
                "Finished" if exit_code == "0" else "Failed (exit 3)"
            )
        assert (
            w.observe(
                [win(state="done", report=report, exit_code=exit_code, finished="1")]
            )
            == []
        )
        assert len(got) == (1 if notified else 0)
        assert len(w.events_since(f"{w.epoch}:0")[0]) == len(got)

    def test_a_human_turn_after_that_notifies_normally(self):
        w = watcher()
        w.observe([])
        assert (
            w.observe([win(state="busy", report="off", exit_code="0", finished="1")])
            == []
        )
        assert (
            w.observe([win(state="done", report="off", exit_code="0", finished="1")])
            == []
        )
        assert (
            w.observe([win(state="busy", report="off", exit_code="0", finished="1")])
            == []
        )
        (ev,) = w.observe(
            [win(state="done", report="off", exit_code="0", finished="1")]
        )
        assert ev.job_id == "" and ev.body.startswith("Finished after 10 s")

    @pytest.mark.parametrize("first", ["idle", "busy"])
    @pytest.mark.parametrize(("report", "exit_code", "notified"), CELLS)
    def test_only_the_first_sweep_is_a_replay(self, first, report, exit_code, notified):
        """The same windows at the watcher's first sweep: left alone, the
        trailing done silent whatever the mode (a replayed busy is not a
        human turn). A turn the human starts afterwards is told normally."""
        w = watcher()
        got: list = []
        w.add_listener(got.append)
        fields = dict(report=report, exit_code=exit_code, finished="1")
        assert w.observe([win(state=first, **fields)]) == []
        assert w.observe([win(state=first, **fields)]) == []
        assert w.observe([win(state="done", **fields)]) == []
        assert w.observe([win(state="done", **fields)]) == []
        assert got == [] and w.events_since(f"{w.epoch}:0")[0] == []
        assert w.observe([win(state="busy", **fields)]) == []
        (ev,) = w.observe([win(state="done", **fields)])
        assert ev.job_id == "" and ev.exit_code is None
        assert ev.body.startswith("Finished after 10 s")
        assert len(got) == 1 and len(w.events_since(f"{w.epoch}:0")[0]) == 1


class TestAHumanTurnDuringTheWait:
    """The human steps into the window while the run's done waits for its
    exit: their turn is tracked and told normally, and the run's own finish
    is still decided by the mode once the code arrives."""

    @pytest.mark.parametrize("report", ["always", "silent", "off"])
    def test_a_turn_begun_before_the_exit_arrives_is_told(self, report):
        w = seen_idle(watcher(), report=report)
        got: list = []
        w.add_listener(got.append)
        w.observe([win(state="busy", report=report)])
        assert w.observe([win(state="done", report=report)]) == []
        assert w.observe([win(state="busy", report=report)]) == []
        # The exit arrives while the human's turn runs: the run's finish
        # (exit 0) is told by the mode, the turn goes on.
        events = w.observe(
            [win(state="busy", report=report, exit_code="0", finished="1")]
        )
        assert [e.job_id for e in events] == (["digest"] if report == "always" else [])
        (ev,) = w.observe(
            [win(state="done", report=report, exit_code="0", finished="1")]
        )
        assert ev.job_id == "" and ev.exit_code is None
        assert ev.body.startswith("Finished after")
        assert len(got) == (2 if report == "always" else 1)

    def test_a_turn_completed_during_the_wait_then_the_runs_failure(self):
        w = seen_idle(watcher(), report="silent")
        got: list = []
        w.add_listener(got.append)
        w.observe([win(state="busy", report="silent")])
        assert w.observe([win(state="done", report="silent")]) == []
        assert w.observe([win(state="busy", report="silent")]) == []
        (turn,) = w.observe([win(state="done", report="silent")])
        assert turn.job_id == "" and turn.body.startswith("Finished after 10 s")
        (finish,) = w.observe(
            [win(state="done", report="silent", exit_code="3", finished="1")]
        )
        assert finish.job_id == "digest" and finish.exit_code == 3
        assert finish.body.startswith("Failed (exit 3) after 10 s")
        assert (
            w.observe([win(state="done", report="silent", exit_code="3", finished="1")])
            == []
        )
        assert len(got) == 2

    def test_a_question_during_the_turn_is_told_once(self):
        w = seen_idle(watcher(), report="off")
        w.observe([win(state="busy", report="off")])
        assert w.observe([win(state="done", report="off")]) == []
        assert w.observe([win(state="busy", report="off")]) == []
        (ask,) = w.observe([win(state="ask", report="off")])
        assert ask.job_id == "" and ask.body.startswith("Needs an answer")
        assert (
            w.observe([win(state="ask", report="off", exit_code="0", finished="1")])
            == []
        )


class TestAFinishStampWithoutAnExit:
    """The supervisor's finish stamp is seen before the exit code: always
    tells a finish of unknown result and corrects it with one failure
    event; silent and off wait for the code and decide then."""

    @pytest.mark.parametrize(
        ("report", "late", "told"),
        [
            ("always", "0", [None]),
            ("always", "3", [None, 3]),
            ("silent", "0", []),
            ("silent", "3", [3]),
            ("off", "0", []),
            ("off", "3", []),
        ],
    )
    def test_the_mode_on_every_late_result(self, report, late, told):
        w = seen_idle(watcher(), report=report)
        got: list = []
        w.add_listener(got.append)
        w.observe([win(state="busy", report=report)])
        first = w.observe([win(state="done", report=report, finished="1")])
        again = w.observe([win(state="done", report=report, finished="1")])
        assert again == []
        last = w.observe(
            [win(state="done", report=report, exit_code=late, finished="1")]
        )
        assert [e.exit_code for e in first + last] == told
        assert all(e.job_id == "digest" for e in first + last)
        assert (
            w.observe([win(state="done", report=report, exit_code=late, finished="1")])
            == []
        )
        assert len(got) == len(told)
        assert len(w.events_since(f"{w.epoch}:0")[0]) == len(told)

    def test_a_stamp_seen_idle_waits_too(self):
        """Busy then idle with the finish stamp but no code under silent:
        held until the code, then decided."""
        w = seen_idle(watcher(), report="silent")
        w.observe([win(state="busy", report="silent")])
        assert w.observe([win(state="idle", report="silent", finished="1")]) == []
        (ev,) = w.observe(
            [win(state="idle", report="silent", exit_code="2", finished="1")]
        )
        assert ev.state == "done" and ev.exit_code == 2
        assert ev.body.startswith("Failed (exit 2) after 10 s")


class TestALateExit:
    """The supervisor is late (adopted at the next scheduler tick): the exit
    arrives after DEFER_SWEEPS. The finish is still reconciled."""

    def sweeps_without_exit(self, w, report):
        from notifications import watcher as mod

        w.observe([win(state="busy", report=report)])
        assert w.observe([win(state="done", report=report)]) == []
        return [
            w.observe([win(state="done", report=report)])
            for _ in range(mod.DEFER_SWEEPS + 2)
        ]

    def test_silent_waits_for_the_code_and_tells_the_failure_once(self):
        w = seen_idle(watcher(), report="silent")
        got: list = []
        w.add_listener(got.append)
        assert all(e == [] for e in self.sweeps_without_exit(w, "silent"))
        (ev,) = w.observe(
            [win(state="done", report="silent", exit_code="3", finished="1")]
        )
        assert ev.exit_code == 3 and ev.job_id == "digest"
        assert ev.body.startswith("Failed (exit 3) after 10 s")
        assert (
            w.observe([win(state="done", report="silent", exit_code="3", finished="1")])
            == []
        )
        assert len(got) == 1

    def test_silent_stays_quiet_on_a_late_success(self):
        w = seen_idle(watcher(), report="silent")
        assert all(e == [] for e in self.sweeps_without_exit(w, "silent"))
        assert (
            w.observe([win(state="done", report="silent", exit_code="0", finished="1")])
            == []
        )

    def test_off_never_tells_a_late_exit(self):
        w = seen_idle(watcher(), report="off")
        assert all(e == [] for e in self.sweeps_without_exit(w, "off"))
        assert (
            w.observe([win(state="done", report="off", exit_code="3", finished="1")])
            == []
        )

    def test_always_corrects_an_unknown_result_with_one_failure_event(self):
        w = seen_idle(watcher())
        sweeps = self.sweeps_without_exit(w, "always")
        told = [e for e in sweeps if e]
        assert len(told) == 1 and told[0][0].exit_code is None
        (ev,) = w.observe([win(state="done", exit_code="3", finished="1")])
        assert ev.exit_code == 3 and ev.body.startswith("Failed (exit 3)")
        assert w.observe([win(state="done", exit_code="3", finished="1")]) == []

    def test_always_says_nothing_more_on_a_late_success(self):
        w = seen_idle(watcher())
        assert len([e for e in self.sweeps_without_exit(w, "always") if e]) == 1
        assert w.observe([win(state="done", exit_code="0", finished="1")]) == []


class TestAHumansLaterTurn:
    def test_a_turn_after_the_finish_notifies_normally_whatever_the_mode(self):
        for report in ("always", "silent", "off"):
            w = seen_idle(watcher(), report=report)
            w.observe([win(state="busy", report=report)])
            w.observe([win(state="done", report=report, exit_code="0", finished="1")])
            # The human steps in: a new turn in the finished window.
            assert (
                w.observe(
                    [win(state="busy", report=report, exit_code="0", finished="1")]
                )
                == []
            )
            (ev,) = w.observe(
                [win(state="done", report=report, exit_code="0", finished="1")]
            )
            assert ev.job_id == "" and ev.exit_code is None
            assert ev.body.startswith("Finished after 10 s")

    def test_a_question_after_the_finish_notifies_even_under_off(self):
        w = seen_idle(watcher(), report="off")
        w.observe([win(state="busy", report="off")])
        assert (
            w.observe([win(state="done", report="off", exit_code="0", finished="1")])
            == []
        )
        w.observe([win(state="busy", report="off", exit_code="0", finished="1")])
        (ev,) = w.observe([win(state="ask", report="off", exit_code="0", finished="1")])
        assert ev.body.startswith("Needs an answer") and ev.job_id == ""

    def test_the_first_sight_of_a_finished_window_is_the_runs_finish(self):
        """Merlin restarted after the run finished: the first sweep sees done
        with the exit and the finish stamped. That is the run's own finish,
        under its report mode."""
        w = watcher()
        assert (
            w.observe([win(state="done", report="silent", exit_code="0", finished="1")])
            == []
        )
        w2 = watcher()
        (ev,) = w2.observe(
            [win(state="done", report="always", exit_code="0", finished="1")]
        )
        assert ev.job_id == "digest"


def test_event_fields_are_only_added():
    w = seen_idle(watcher())
    w.observe([win(state="busy")])
    (ev,) = w.observe([win(state="done", exit_code="0")])
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
        "machine",
        "busy_seconds",
        "snippet",
        "title",
        "body",
        "quiet",
        "job_id",
        "exit_code",
    ):
        assert key in d
    assert d["job_id"] == "digest" and d["exit_code"] == 0


def test_an_ordinary_window_is_untouched_by_the_job_rule():
    w = seen_idle(watcher(), report="", job_id="")
    plain = win(state="busy", report="", job_id="")
    w.observe([plain])
    (ev,) = w.observe([win(state="done", report="", job_id="")])
    assert ev.job_id == "" and ev.exit_code is None


def test_failure_wording():
    assert (
        compose_body("done", 90, "last line", 3)
        == "Failed (exit 3) after 2 min: last line"
    )
    assert compose_body("done", None, "", 124) == "Failed (exit 124)"
    assert compose_body("done", 40, "ok", 0) == "Finished after 40 s: ok"
    assert compose_body("ask", 40, "which?", 0) == "Needs an answer: which?"


def test_a_powerline_shell_prompt_is_chrome():
    """A job window's shell draws a powerline prompt once the run is over:
    the snippet keeps the command's last line, not the prompt."""
    from notifications.content import clean_snippet

    pane = "boom\n░▒▓  │  ~/dev ▓▒░·····░▒▓ 22:14 ▓▒░\n❯ \n"
    assert clean_snippet(pane) == "boom"
