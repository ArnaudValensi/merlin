"""What every scheduler tick does besides dispatching: orphans and windows.

**Orphans.** A supervisor can die with its run: a Merlin restart kills a
webhook thread, a reboot kills everything. A run directory with no
``result.json`` whose job flock is free is finalized from what is left: its
``exit`` file, its window's state, or exit 130 if the window is gone. The
flock is what keeps two overlapping ticks from finalizing one run twice, and
``result.json`` is re-checked under it, so nothing is recorded twice. A run
still supervised holds its flock and is left alone.

**Windows do not pile up.** Each tick closes ``merlin-jobs`` windows and
removes their run directories:

- a window a tmux client is showing right now is never closed;
- finished with exit 0 and no pane activity since: closed after
  ``SUCCESS_TTL`` (1 hour), or at the next run of the same job, whichever
  comes first;
- failed, or activity after the finish (someone stepped in): closed
  ``ACTIVITY_TTL`` (24 hours) after the last activity;
- closing never touches a window without ``@job_run``, nor any other
  session.

A recorded run whose window is already gone loses its directory at once.
A tick with no ``merlin-jobs`` session and no run directory does no work.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from board import sweep
from job import runs, supervisor
from job.runs import Run
from job.state import acquire_job_lock, release_job_lock

logger = logging.getLogger("merlin.job")

SUCCESS_TTL = 3600.0
ACTIVITY_TTL = 86400.0
# The finish itself writes to the pane (the agent's last render, the shell
# prompt run.sh execs into): activity within this many seconds of the finish
# is the run's own, not a human's.
ACTIVITY_SLACK = 5.0
# A directory without a meta.json this old was abandoned mid-creation.
STRAY_TTL = 3600.0

_WINDOW_FIELDS = (
    "@job_run",
    "@job_id",
    "@job_exit",
    "@job_finished",
    "@job_started",
    "window_activity",
)


def tick(*, now: float | None = None, record=None) -> dict[str, int]:
    """Finalize orphans and close windows past their retention. ``record``
    is the recorder the orphan path calls (``runner.record_run`` by
    default; tests pass their own). Never raises: a tmux hiccup is retried
    on the next tick."""
    now = time.time() if now is None else now
    counts = {"finalized": 0, "closed": 0, "removed": 0}
    dirs = runs.run_dirs()
    rows = sweep.list_window_options(runs.JOBS_SESSION, _WINDOW_FIELDS)
    if not dirs and not rows:
        return counts
    try:
        counts["finalized"] = finalize_orphans(dirs, rows, now=now, record=record)
    except Exception:
        logger.exception("Finalizing orphan job runs failed")
    try:
        closed, removed = close_windows(
            runs.run_dirs(), rows if rows is not None else [], now=now
        )
        counts["closed"], counts["removed"] = closed, removed
    except Exception:
        logger.exception("Closing job windows failed")
    return counts


# ---------------------------------------------------------------------------
# Orphans
# ---------------------------------------------------------------------------


def orphan_exit(run: Run, opts: dict[str, str] | None, *, now: float) -> int | None:
    """What an unsupervised run's remains say, or None while it still goes
    on. The window's state reads as the supervisor would: ``done`` finishes
    an agent run, and so does an ``idle`` older than the start watchdog
    (nobody saw it busy, so nobody can tell the difference). The deadline
    applies to an orphan too."""
    code = runs.read_exit(run.dir)
    if code is not None:
        return code
    if opts is None:
        return 130
    if run.is_agent:
        state = opts.get("@agent_state", "").strip().lower()
        if state == "done":
            return 0
        if state == "idle" and now - run.started >= supervisor.START_WATCHDOG:
            return 0
    if now - run.started > run.deadline:
        return 124
    return None


def finalize_orphans(
    dirs: list[Path], rows: list[dict] | None, *, now: float, record=None
) -> int:
    if record is None:
        from job.runner import record_run

        record = record_run
    windows = {r["@job_run"]: r for r in (rows or []) if r.get("@job_run")}
    count = 0
    for directory in dirs:
        run = runs.read_meta(directory)
        if run is None:
            if _age(directory) > STRAY_TTL:
                runs.remove(directory)
            continue
        if runs.is_recorded(directory):
            continue
        lock = acquire_job_lock(run.job_id)
        if lock is None:
            continue  # supervised: its thread holds the flock
        try:
            if runs.is_recorded(directory):
                continue
            opts = None
            if run.target and run.window_id in {
                r["window_id"] for r in windows.values()
            }:
                opts = sweep.window_options(run.target, supervisor.WINDOW_FIELDS)
            code = orphan_exit(run, opts, now=now)
            if code is None:
                continue
            note = "finalized after its supervisor died"
            if code == 124:
                note = f"deadline of {int(run.deadline)} s passed, {note}"
                supervisor.end_run(run, code)
            elif code == 130 and opts is None:
                note = f"window closed before the run ended, {note}"
            outcome = supervisor.finish(run, code, note=note, now=now)
            record(run, outcome)
            count += 1
        finally:
            release_job_lock(lock)
    return count


def _age(directory: Path) -> float:
    try:
        return time.time() - directory.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def should_close(
    row: dict[str, str],
    *,
    now: float,
    displayed: set[str],
    newer_run_started: float | None,
) -> bool:
    """The retention rule for one finished job window (pure)."""
    target = f"{runs.JOBS_SESSION}:{row['window_id']}"
    if target in displayed:
        return False
    finished_s = row.get("@job_finished", "")
    if not finished_s.isdigit():
        return False  # still running, or an orphan the next tick finalizes
    finished = float(finished_s)
    activity_s = row.get("window_activity", "")
    activity = float(activity_s) if activity_s.isdigit() else 0.0
    stepped_in = activity > finished + ACTIVITY_SLACK
    succeeded = row.get("@job_exit", "") == "0"
    if succeeded and not stepped_in:
        if now - finished >= SUCCESS_TTL:
            return True
        return newer_run_started is not None and newer_run_started > finished
    last = max(activity, finished)
    return now - last >= ACTIVITY_TTL


def close_windows(dirs: list[Path], rows: list[dict], *, now: float) -> tuple[int, int]:
    """Close the windows past their retention and remove their run
    directories; remove the directory of a recorded run whose window is
    gone. Returns ``(closed, removed)``."""
    displayed = sweep.displayed_windows()
    by_run = {r["@job_run"]: r for r in rows if r.get("@job_run")}
    # The newest start per job, from the windows and the run directories.
    # Whole seconds throughout, like the stamped options, so a run's own
    # start never reads as newer than itself.
    newest: dict[str, float] = {}
    for r in rows:
        started = r.get("@job_started", "")
        if r.get("@job_id") and started.isdigit():
            newest[r["@job_id"]] = max(newest.get(r["@job_id"], 0.0), float(started))
    metas: dict[str, Run] = {}
    for directory in dirs:
        run = runs.read_meta(directory)
        if run is None:
            continue
        metas[run.run_id] = run
        newest[run.job_id] = max(newest.get(run.job_id, 0.0), float(int(run.started)))

    closed = removed = 0
    for run_id, row in by_run.items():
        job_id = row.get("@job_id", "")
        newer = newest.get(job_id)
        finished_s = row.get("@job_finished", "")
        own_start = (
            float(row["@job_started"]) if row.get("@job_started", "").isdigit() else 0.0
        )
        newer_started = newer if newer is not None and newer > own_start else None
        if not should_close(
            row, now=now, displayed=displayed, newer_run_started=newer_started
        ):
            continue
        if not finished_s.isdigit():
            continue
        if sweep.kill_window(runs.JOBS_SESSION, row["window_id"]):
            closed += 1
            run = metas.pop(run_id, None)
            if run is not None:
                runs.remove(run.dir)
                removed += 1
    for run_id, run in metas.items():
        if run_id not in by_run and runs.is_recorded(run.dir):
            runs.remove(run.dir)
            removed += 1
    return closed, removed
