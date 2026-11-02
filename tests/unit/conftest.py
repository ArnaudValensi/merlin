"""Shared fixtures of the unit suite."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def fake_launch(monkeypatch):
    """Run jobs without tmux: the run directory is still created (under the
    test's MERLIN_HOME), but the window is not opened and the supervisor
    returns a scripted outcome at once.

    The returned recorder holds the launched ``runs`` and the outcome every
    run gets: ``exit_code``, ``output``, ``delay`` (seconds the fake
    supervisor sleeps, for parallelism tests) and ``fail`` (an exception the
    fake supervisor raises). Agents are considered installed with their
    hooks.
    """
    from job import agents, runner, runs, supervisor

    rec = SimpleNamespace(runs=[], exit_code=0, output="ok", delay=0.0, fail=None)

    def fake_open(run):
        run.target = f"{runs.JOBS_SESSION}:@{len(rec.runs) + 1}"
        rec.runs.append(run)
        runs.write_meta(run)
        return run.target

    def fake_supervise(run, **_kw):
        if rec.fail is not None:
            raise rec.fail
        if rec.delay:
            time.sleep(rec.delay)
        return supervisor.Outcome(
            exit_code=rec.exit_code,
            finished=max(run.started, time.time()),
            output=rec.output,
        )

    monkeypatch.setattr(runner.runs, "launch", fake_open)
    monkeypatch.setattr(runner.supervisor, "supervise", fake_supervise)
    monkeypatch.setattr(runs.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(agents, "launch_problem", lambda agent: None)
    monkeypatch.setattr(
        runner.retention,
        "tick",
        lambda **kw: {"finalized": 0, "closed": 0, "removed": 0},
    )
    return rec
