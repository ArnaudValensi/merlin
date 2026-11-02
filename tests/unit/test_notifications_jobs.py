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
        """done (no exit) then ask: the question is told under the mode; the
        exit then decides the finish, and the ask is not told again."""
        w = seen_idle(watcher(), report="silent")
        w.observe([win(state="busy", report="silent")])
        assert w.observe([win(state="done", report="silent")]) == []
        (ask,) = w.observe([win(state="ask", report="silent")])
        assert ask.state == "ask" and ask.job_id == "digest"
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
