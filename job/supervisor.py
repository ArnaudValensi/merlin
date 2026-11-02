"""The supervisor: knowing that a run finished.

The thread holding the job's flock (the runner's worker, or the webhook
path's thread) polls the run every ``POLL_INTERVAL`` seconds. A run is
finished when any of these holds, first match wins:

1. The ``exit`` file exists: the exit code is read from it.
2. Agent only: ``@agent_state`` is ``done``, or it is ``idle`` after the
   supervisor saw it ``busy``: exit 0. The agent keeps running: finishing a
   run does not end the conversation, and a human may step in.
3. The deadline passed: exit 124. The supervisor writes the ``exit`` file and
   ``@job_exit`` first, then sends SIGTERM then SIGKILL to the job's process
   group, so ``run.sh`` stamps the end and the window stays.
4. The window vanished: exit 130.

An agent still not ``busy`` ``START_WATCHDOG`` seconds after the launch is
stuck on something no hook reports (a trust or bypass dialog on a fresh
machine): the supervisor stamps ``@agent_state ask`` so the ordinary "Needs
an answer" notification fires, and keeps waiting. The deadline still bounds
the run.

At the finish the supervisor stamps ``@job_exit`` (when ``run.sh`` has not)
and ``@job_finished`` on the window, and reads the record's output: the
``output.log`` of a command, the whole pane of an agent.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass

from board import sweep
from job import runs
from job.runs import Run

logger = logging.getLogger("merlin.job")

POLL_INTERVAL = 2.0
START_WATCHDOG = 120.0
# How long SIGTERM gets before SIGKILL on a deadline (seconds).
TERM_GRACE = 5.0

WINDOW_FIELDS = ("@agent_state", "@job_exit")


@dataclass
class Outcome:
    exit_code: int
    finished: float  # epoch seconds
    output: str
    note: str = ""  # why the run ended, when not by its own exit


def finish_from_window(
    run: Run, opts: dict[str, str] | None, *, seen_busy: bool
) -> int | None:
    """Rules 2 and 4 on one read of the window (rule 1, the ``exit`` file, is
    checked by the caller first). None while the run goes on."""
    if opts is None:
        return 130
    if run.is_agent:
        state = opts.get("@agent_state", "").strip().lower()
        if state == "done" or (state == "idle" and seen_busy):
            return 0
    return None


def supervise(
    run: Run,
    *,
    interval: float | None = None,
    watchdog: float | None = None,
    term_grace: float | None = None,
) -> Outcome:
    """Wait for the run to finish and return its outcome. Blocking: the
    caller runs it in the thread that holds the job's flock. The timings
    default to the module constants, read at call time."""
    interval = POLL_INTERVAL if interval is None else interval
    watchdog = START_WATCHDOG if watchdog is None else watchdog
    term_grace = TERM_GRACE if term_grace is None else term_grace
    seen_busy = False
    asked = False
    note = ""
    while True:
        code = runs.read_exit(run.dir)
        if code is not None:
            break
        opts = sweep.window_options(run.target, WINDOW_FIELDS)
        if opts is not None and run.is_agent:
            state = opts.get("@agent_state", "").strip().lower()
            if state == "busy":
                seen_busy = True
        code = finish_from_window(run, opts, seen_busy=seen_busy)
        if code is not None:
            if code == 130:
                note = "window closed before the run ended"
            break
        now = time.time()
        if now - run.started > run.deadline:
            code = 124
            note = f"deadline of {int(run.deadline)} s passed"
            end_run(run, code, term_grace=term_grace)
            break
        if (
            run.is_agent
            and not seen_busy
            and not asked
            and opts is not None
            and opts.get("@agent_state", "").strip().lower() != "ask"
            and now - run.started >= watchdog
        ):
            asked = True
            logger.warning(
                "Job %s run %s not busy after %.0fs, marking it as asking",
                run.job_id,
                run.run_id,
                watchdog,
            )
            sweep.set_window_option(run.target, "@agent_state", "ask")
        time.sleep(interval)
    return finish(run, code, note=note)


def end_run(run: Run, code: int, *, term_grace: float | None = None) -> None:
    """Record ``code`` as the run's exit (file and window option) and end the
    job's process group, SIGTERM then SIGKILL. ``run.sh`` then stamps the
    end and keeps the window."""
    term_grace = TERM_GRACE if term_grace is None else term_grace
    runs.write_exit_once(run.dir, code)
    if run.target:
        sweep.set_window_option(run.target, "@job_exit", str(code))
    pgid = runs.read_pgid(run.dir)
    if pgid is None:
        return
    if _signal_group(pgid, signal.SIGTERM):
        deadline = time.monotonic() + term_grace
        while time.monotonic() < deadline and _group_alive(pgid):
            time.sleep(0.1)
        if _group_alive(pgid):
            _signal_group(pgid, signal.SIGKILL)


def _signal_group(pgid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        logger.warning("Cannot signal job process group %d", pgid)
        return False


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def finish(run: Run, code: int, *, note: str = "", now: float | None = None) -> Outcome:
    """Stamp the finish on the window (``@job_exit`` unless ``run.sh`` wrote
    it, ``@job_finished``) and gather the record's output."""
    finished = time.time() if now is None else now
    opts = sweep.window_options(run.target, ("@job_exit",)) if run.target else None
    if opts is not None:
        stamps: dict[str, str] = {}
        if not opts.get("@job_exit"):
            stamps["@job_exit"] = str(code)
        stamps["@job_finished"] = str(int(finished))
        sweep.set_window_options(run.target, stamps)
    output = read_output(run, alive=opts is not None)
    if note:
        output = (output + "\n\n" if output else "") + f"[merlin: {note}]"
    return Outcome(exit_code=code, finished=finished, output=output, note=note)


def read_output(run: Run, *, alive: bool) -> str:
    """A command's ``output.log``, or the plain text of the agent's whole
    pane (``capture-pane -p -S -``) while the window exists."""
    if not run.is_agent:
        return runs.read_output(run.dir)
    if not alive or not run.target:
        return ""
    text = sweep.capture_pane(
        runs.JOBS_SESSION, run.window_id, timeout=10.0, history=True
    )
    return (text or "").rstrip("\n")
