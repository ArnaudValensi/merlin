"""What every scheduler tick does besides dispatching: orphans and windows.

**Orphans.** A supervisor can die with its run: a Merlin restart kills a
webhook thread, a reboot kills everything. A run directory without its
recorded marker, whose job flock is free, is looked at under that flock:

- an outcome already published is materialized again (history and the run
  log, idempotent by run id): the record was interrupted between its steps;
- an ``exit`` file is a finished run: recorded from it;
- a window tmux confirmed gone, or whose id now carries another run's
  ``@job_run`` (ids are reused after a server restart), is exit 130;
- anything else is a run still going on: it is handed back to the
  dispatcher, which adopts it (``runner.adopt_run``) and supervises it to
  its end with the window's own evidence, under the flock, so the watchdog
  and the deadline apply to it and no second run of the job can start.

The flock is what keeps two overlapping ticks from recording one run twice,
and the marker is re-checked under it. A run still supervised holds its
flock and is left alone. An observation that could not be made (tmux did
not answer) finalizes nothing. A directory without ``meta.json`` older than
an hour was abandoned mid-creation and is removed.

**Windows do not pile up.** Each tick closes ``merlin-jobs`` windows and
removes their run directories:

- a window a tmux client is showing right now is never closed, and when
  the client list could not be read no window is closed at all;
- a window someone stepped into (``@job_stepped_in``: Enter at the closing
  prompt, a prompt after the run's first, Esc on it), or kept by its job's choice
  (``@job_keep_window``), is theirs: never closed, and the workspace snapshot
  brings it back after a reboot;
- finished with exit 0: closed at the next run of the same job, or
  ``WINDOW_TTL`` (24 hours) after its last activity, whichever comes first;
- failed: closed ``WINDOW_TTL`` after its last activity;
- closing never touches a window without ``@job_run``, nor any other
  session, and the window is read again right before the kill: it must
  still carry the same ``@job_run`` and still be shown by nobody.

A recorded run whose window tmux confirmed gone loses its directory at
once. A tick with no ``merlin-jobs`` session and no run directory does no
work.
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

# Long enough to find a night's run in the morning; an idle agent holds a few
# hundred MB, so a rare job's window does not stay for its whole period.
WINDOW_TTL = 86400.0
# A directory without a meta.json this old was abandoned mid-creation.
STRAY_TTL = 3600.0

_WINDOW_FIELDS = (
    "@job_run",
    "@job_id",
    "@job_exit",
    "@job_finished",
    "@job_started",
    "@job_stepped_in",
    "@job_keep_window",
    "window_activity",
)


def tick(*, now: float | None = None, record=None) -> tuple[dict[str, int], list[Run]]:
    """Finalize what can be finalized and close windows past their
    retention. Returns the counts and the unfinished runs to adopt (their
    flock free, their window going on). ``record`` is the materializer the
    orphan path calls (``runner.materialize`` by default). Never raises: a
    tmux hiccup is retried on the next tick."""
    now = time.time() if now is None else now
    counts = {"finalized": 0, "closed": 0, "removed": 0}
    dirs = runs.run_dirs()
    status, rows = sweep.list_window_options_checked(runs.JOBS_SESSION, _WINDOW_FIELDS)
    if not dirs and status == "gone":
        return counts, []
    adoptable: list[Run] = []
    try:
        counts["finalized"], adoptable = finalize_orphans(
            dirs, status, rows, now=now, record=record
        )
    except Exception:
        logger.exception("Finalizing orphan job runs failed")
    try:
        closed, removed = close_windows(runs.run_dirs(), status, rows, now=now)
        counts["closed"], counts["removed"] = closed, removed
    except Exception:
        logger.exception("Closing job windows failed")
    return counts, adoptable


# ---------------------------------------------------------------------------
# Orphans
# ---------------------------------------------------------------------------


def orphan_exit(run: Run, status: str, present: bool) -> int | None:
    """What an unsupervised run's remains say: its exit file, or 130 when
    tmux confirmed its window gone (``present`` is whether the listing holds
    a window carrying this run's ``@job_run``). None while it goes on, or
    while the window could not be observed."""
    code = runs.read_exit(run.dir)
    if code is not None:
        return code
    if status == "unavailable":
        return None
    if status == "gone" or not present:
        return 130
    return None


def finalize_orphans(
    dirs: list[Path], status: str, rows: list[dict], *, now: float, record=None
) -> tuple[int, list[Run]]:
    if record is None:
        from job.runner import materialize

        record = materialize
    owned = {r["@job_run"]: r["window_id"] for r in rows if r.get("@job_run")}
    count = 0
    adoptable: list[Run] = []
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
            canonical = runs.read_outcome(directory)
            if canonical is not None:
                record(run, canonical)  # the record was interrupted
                count += 1
                continue
            if not run.target:
                # The launch crashed before the metadata got its target: the
                # window carrying this run's @job_run, if the launch got that
                # far, is the run's; else an old enough preparation is a
                # failed launch, recorded and removed.
                target = owned.get(run.run_id)
                if target and status == "ok":
                    run.target = f"{runs.JOBS_SESSION}:{target}"
                    runs.write_meta(run)
                elif (
                    status != "unavailable" and now - run.started > runs.PREPARATION_TTL
                ):
                    outcome = supervisor.Outcome(
                        exit_code=1,
                        finished=now,
                        output="[merlin: the launch was interrupted before the window opened]",
                        note="launch interrupted",
                    )
                    from job.runner import record_run

                    record_run(run, outcome)
                    count += 1
                    continue
                else:
                    continue
            present = owned.get(run.run_id) == run.window_id
            code = orphan_exit(run, status, present)
            if code is None:
                if status == "ok" and present:
                    adoptable.append(run)
                continue
            if runs.read_exit(directory) is not None and not supervisor.ensure_ended(
                run
            ):
                continue  # the timed-out job is still alive: next tick
            note = "finalized after its supervisor died"
            if code == 130:
                note = f"window closed before the run ended, {note}"
            outcome = supervisor.finish(run, code, note=note, now=now)
            from job.runner import record_run

            record_run(run, outcome)
            count += 1
        finally:
            release_job_lock(lock)
    return count, adoptable


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
    if row.get("@job_stepped_in") or row.get("@job_keep_window"):
        return False  # it is someone's now
    last = max(activity, finished)
    if now - last >= WINDOW_TTL:
        return True
    # Output alone says nothing here: an agent repaints for some twenty seconds
    # after its turn, so reading that as someone there would keep every run's
    # window, and its agent, for the whole day.
    return (
        row.get("@job_exit", "") == "0"
        and newer_run_started is not None
        and newer_run_started > finished
    )


def close_windows(
    dirs: list[Path], status: str, rows: list[dict], *, now: float
) -> tuple[int, int]:
    """Close the windows past their retention and remove their run
    directories; remove the directory of a recorded run whose window tmux
    confirmed gone. Returns ``(closed, removed)``. Nothing is closed when
    the window listing or the client list could not be read."""
    if status == "unavailable":
        return 0, 0
    client_status, displayed = sweep.displayed_windows_checked()
    by_run = {r["@job_run"]: r for r in rows if r.get("@job_run")}
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
    if client_status == "ok":
        for run_id, row in by_run.items():
            job_id = row.get("@job_id", "")
            newer = newest.get(job_id)
            own_start = (
                float(row["@job_started"])
                if row.get("@job_started", "").isdigit()
                else 0.0
            )
            newer_started = newer if newer is not None and newer > own_start else None
            if not should_close(
                row, now=now, displayed=displayed, newer_run_started=newer_started
            ):
                continue
            if not _still_closable(row, now=now, newer_run_started=newer_started):
                continue
            if sweep.kill_window(runs.JOBS_SESSION, row["window_id"]):
                closed += 1
                run = metas.pop(run_id, None)
                # The directory goes with the window only once its record is
                # complete: an outcome not yet materialized (a write that
                # keeps failing) is kept for the recovery to retry.
                if run is not None and runs.is_recorded(run.dir):
                    runs.remove(run.dir)
                    removed += 1
    for run_id, run in metas.items():
        if run_id not in by_run and runs.is_recorded(run.dir):
            runs.remove(run.dir)
            removed += 1
    return closed, removed


def _still_closable(
    row: dict[str, str], *, now: float, newer_run_started: float | None
) -> bool:
    """Read the window again right before the kill and decide on what it
    says now: it must still carry the same run, its retention fields
    (someone may have stepped in since the sweep) must still say close, and
    nobody may be showing it. A read that fails closes nothing."""
    target = f"{runs.JOBS_SESSION}:{row['window_id']}"
    status, opts = sweep.window_options_checked(target, _WINDOW_FIELDS)
    if status != "ok" or opts is None or opts.get("@job_run") != row["@job_run"]:
        return False
    client_status, displayed = sweep.displayed_windows_checked()
    if client_status != "ok":
        return False
    fresh = {"window_id": row["window_id"], **opts}
    return should_close(
        fresh, now=now, displayed=displayed, newer_run_started=newer_run_started
    )
