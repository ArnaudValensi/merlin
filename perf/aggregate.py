"""Pure aggregation of invocation events into chartable performance data.

This is the single, testable home for the math that the bot performance page
currently does inline in client-side JavaScript. It takes already-read
:class:`InvocationEvent` models and returns a :class:`PerformanceData` model
that FastAPI serializes directly (and documents in OpenAPI).

Design rules (see epics/cli/cron-performance/requirements.md (pre-rename epic name), R3):
  - No I/O and no internal clock reads. ``now`` is passed in so tests are
    deterministic and there is an explicit seam for future time-relative views.
  - ``caller`` strings pass through verbatim (including ids of deleted jobs).
  - Events with ``cost_usd is None`` count toward duration aggregates but are
    excluded from cost aggregates.
"""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel

from lib.event_log import InvocationEvent

# Rounding precision for display-oriented numbers.
_SECONDS_PRECISION = 3
_USD_PRECISION = 6


class TimeseriesPoint(BaseModel):
    """One raw invocation, for the execution-time scatter chart."""

    timestamp: str
    duration: float
    exit_code: int
    caller: str


class SuccessRate(BaseModel):
    """Success / error split for the donut chart."""

    success: int
    error: int
    total: int


class JobDuration(BaseModel):
    """Per-caller duration stats for the execution-time-by-job bar chart."""

    caller: str
    count: int
    avg_seconds: float
    p95_seconds: float


class JobCost(BaseModel):
    """Per-caller cost stats for the cost-by-job bar chart."""

    caller: str
    count: int
    total_usd: float
    avg_usd: float


class PerformanceData(BaseModel):
    """The full payload returned by the performance endpoint."""

    timeseries: list[TimeseriesPoint]
    success_rate: SuccessRate
    by_job_duration: list[JobDuration]
    by_job_cost: list[JobCost]


def _p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile.

    Uses ``sorted_values[ceil(0.95 * n) - 1]``. Empty list -> 0.0; a single
    element returns that element.
    """
    n = len(values)
    if n == 0:
        return 0.0
    ordered = sorted(values)
    index = math.ceil(0.95 * n) - 1
    return ordered[index]


def aggregate_invocations(
    events: list[InvocationEvent],
    now: datetime,
) -> PerformanceData:
    """Aggregate invocation events into chartable performance data.

    Args:
        events: Invocation events to aggregate (already filtered to the desired
            caller set by the caller, e.g. ``job-*`` for the jobs page).
        now: The reference "current time". v1 produces no time-relative
            sections, so this is currently unused; it stays in the signature to
            forbid internal clock reads (determinism) and to reserve the seam
            for future relative views. Passing it never changes today's output.

    Returns:
        A :class:`PerformanceData` model. Empty ``events`` yields zeroed totals
        and empty lists (the endpoint still returns 200, not 404).
    """
    del now  # reserved; intentionally unused in v1 (see docstring)

    timeseries = [
        TimeseriesPoint(
            timestamp=e.timestamp,
            duration=e.duration,
            exit_code=e.exit_code,
            caller=e.caller or "",
        )
        for e in events
    ]

    success = sum(1 for e in events if e.exit_code == 0)
    error = len(events) - success
    success_rate = SuccessRate(success=success, error=error, total=len(events))

    # Group durations by caller (every event participates).
    durations_by_caller: dict[str, list[float]] = {}
    # Group costs by caller (only events that have a cost participate).
    costs_by_caller: dict[str, list[float]] = {}
    for e in events:
        caller = e.caller or ""
        durations_by_caller.setdefault(caller, []).append(e.duration)
        if e.cost_usd is not None:
            costs_by_caller.setdefault(caller, []).append(e.cost_usd)

    by_job_duration = [
        JobDuration(
            caller=caller,
            count=len(durations),
            avg_seconds=round(sum(durations) / len(durations), _SECONDS_PRECISION),
            p95_seconds=round(_p95(durations), _SECONDS_PRECISION),
        )
        for caller, durations in durations_by_caller.items()
    ]
    by_job_duration.sort(key=lambda j: (-j.count, j.caller))

    by_job_cost = [
        JobCost(
            caller=caller,
            count=len(costs),
            total_usd=round(sum(costs), _USD_PRECISION),
            avg_usd=round(sum(costs) / len(costs), _USD_PRECISION),
        )
        for caller, costs in costs_by_caller.items()
    ]
    by_job_cost.sort(key=lambda j: (-j.total_usd, j.caller))

    return PerformanceData(
        timeseries=timeseries,
        success_rate=success_rate,
        by_job_duration=by_job_duration,
        by_job_cost=by_job_cost,
    )
