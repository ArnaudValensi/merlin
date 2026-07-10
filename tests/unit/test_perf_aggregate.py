"""Tests for perf/aggregate.py — the pure invocation aggregator."""

from datetime import datetime, timedelta, timezone

import pytest

from lib.event_log import InvocationEvent
from perf.aggregate import (
    PerformanceData,
    _p95,
    aggregate_invocations,
)

NOW = datetime(2026, 5, 31, tzinfo=timezone.utc)


def _inv(
    caller: str = "job-foo",
    duration: float = 1.0,
    cost_usd: float | None = 0.01,
    exit_code: int = 0,
    timestamp: str = "2026-05-30T12:00:00+00:00",
) -> InvocationEvent:
    return InvocationEvent(
        type="invocation",
        timestamp=timestamp,
        caller=caller,
        duration=duration,
        cost_usd=cost_usd,
        exit_code=exit_code,
    )


# ---------------------------------------------------------------------------
# Empty / single
# ---------------------------------------------------------------------------


def test_aggregate_empty_returns_zeroed_model():
    data = aggregate_invocations([], NOW)
    assert isinstance(data, PerformanceData)
    assert data.timeseries == []
    assert data.by_job_duration == []
    assert data.by_job_cost == []
    assert data.success_rate.success == 0
    assert data.success_rate.error == 0
    assert data.success_rate.total == 0


def test_aggregate_single_event_groups_correctly():
    data = aggregate_invocations(
        [_inv(caller="job-foo", duration=2.0, cost_usd=0.01, exit_code=0)], NOW
    )

    assert data.success_rate.success == 1
    assert data.success_rate.error == 0
    assert data.success_rate.total == 1

    assert len(data.by_job_duration) == 1
    d = data.by_job_duration[0]
    assert d.caller == "job-foo"
    assert d.count == 1
    assert d.avg_seconds == pytest.approx(2.0)
    assert d.p95_seconds == pytest.approx(2.0)

    assert len(data.by_job_cost) == 1
    c = data.by_job_cost[0]
    assert c.caller == "job-foo"
    assert c.count == 1
    assert c.total_usd == pytest.approx(0.01)
    assert c.avg_usd == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# P95 (nearest-rank)
# ---------------------------------------------------------------------------


def test_aggregate_p95_at_boundaries():
    # Empty and single
    assert _p95([]) == 0.0
    assert _p95([42.0]) == 42.0

    # len 2: ceil(1.9)=2 -> index 1 -> max
    assert _p95([10.0, 20.0]) == 20.0

    # len 19: ceil(18.05)=19 -> index 18 -> the 19th value
    assert _p95([float(i) for i in range(1, 20)]) == 19.0

    # len 20: ceil(19.0)=19 -> index 18 -> the 19th value
    assert _p95([float(i) for i in range(1, 21)]) == 19.0

    # len 21: ceil(19.95)=20 -> index 19 -> the 20th value
    assert _p95([float(i) for i in range(1, 22)]) == 20.0


def test_aggregate_p95_independent_of_input_order():
    """Nearest-rank must sort internally, so order in must not change the result."""
    ascending = [float(i) for i in range(1, 21)]
    shuffled = ascending[::-1]
    assert _p95(shuffled) == _p95(ascending) == 19.0


# ---------------------------------------------------------------------------
# Grouping / sorting / labels
# ---------------------------------------------------------------------------


def test_aggregate_groups_by_caller():
    data = aggregate_invocations(
        [
            _inv(caller="job-a"),
            _inv(caller="job-b"),
            _inv(caller="job-a"),
        ],
        NOW,
    )
    by_caller = {d.caller: d.count for d in data.by_job_duration}
    assert by_caller == {"job-a": 2, "job-b": 1}


def test_aggregate_success_rate_denominator():
    events = [_inv(exit_code=0) for _ in range(7)] + [
        _inv(exit_code=1) for _ in range(3)
    ]
    data = aggregate_invocations(events, NOW)
    assert data.success_rate.success == 7
    assert data.success_rate.error == 3
    assert data.success_rate.total == 10


def test_aggregate_excludes_none_cost_from_cost_totals():
    data = aggregate_invocations(
        [
            _inv(caller="job-priced", duration=1.0, cost_usd=0.05),
            _inv(caller="job-free", duration=2.0, cost_usd=None),
        ],
        NOW,
    )

    # Both contribute to duration aggregates.
    duration_callers = {d.caller for d in data.by_job_duration}
    assert duration_callers == {"job-priced", "job-free"}

    # Only the priced one appears in cost aggregates.
    cost_callers = {c.caller for c in data.by_job_cost}
    assert cost_callers == {"job-priced"}
    assert data.by_job_cost[0].total_usd == pytest.approx(0.05)
    assert data.by_job_cost[0].count == 1


def test_aggregate_excludes_none_cost_but_keeps_other_costs_for_same_caller():
    """A caller with mixed None/priced runs: cost count only counts priced runs."""
    data = aggregate_invocations(
        [
            _inv(caller="job-mixed", duration=1.0, cost_usd=0.10),
            _inv(caller="job-mixed", duration=1.0, cost_usd=None),
            _inv(caller="job-mixed", duration=1.0, cost_usd=0.20),
        ],
        NOW,
    )
    assert data.by_job_duration[0].count == 3  # all runs
    cost = data.by_job_cost[0]
    assert cost.count == 2  # only priced runs
    assert cost.total_usd == pytest.approx(0.30)
    assert cost.avg_usd == pytest.approx(0.15)


def test_aggregate_preserves_deleted_job_labels():
    data = aggregate_invocations([_inv(caller="job-no-longer-exists")], NOW)
    assert data.by_job_duration[0].caller == "job-no-longer-exists"
    assert data.by_job_cost[0].caller == "job-no-longer-exists"
    assert data.timeseries[0].caller == "job-no-longer-exists"


def test_aggregate_timeseries_preserves_raw_points():
    events = [
        _inv(
            caller=f"job-{i}",
            duration=float(i),
            timestamp=f"2026-05-{10 + i:02d}T12:00:00+00:00",
        )
        for i in range(5)
    ]
    data = aggregate_invocations(events, NOW)

    assert len(data.timeseries) == 5
    assert [p.timestamp for p in data.timeseries] == [e.timestamp for e in events]
    assert [p.duration for p in data.timeseries] == [e.duration for e in events]


def test_aggregate_now_parameter_is_used_not_clock():
    """Output must depend only on events, never on the wall clock or `now`."""
    events = [_inv(caller="job-a", duration=3.0), _inv(caller="job-b", duration=1.0)]

    past = aggregate_invocations(events, NOW - timedelta(days=30))
    future = aggregate_invocations(events, NOW + timedelta(days=365))

    # Same events -> identical result regardless of the `now` passed.
    assert past == future
    # And the timeseries timestamps are exactly the event timestamps (no clock).
    assert [p.timestamp for p in past.timeseries] == [e.timestamp for e in events]


def test_aggregate_by_job_sorted_descending():
    events = (
        [_inv(caller="job-one")] * 1
        + [_inv(caller="job-five")] * 5
        + [_inv(caller="job-two")] * 2
    )
    data = aggregate_invocations(events, NOW)
    assert [d.count for d in data.by_job_duration] == [5, 2, 1]
    assert [d.caller for d in data.by_job_duration] == [
        "job-five",
        "job-two",
        "job-one",
    ]


def test_aggregate_by_job_cost_sorted_by_total_descending():
    events = [
        _inv(caller="job-cheap", cost_usd=0.01),
        _inv(caller="job-pricey", cost_usd=0.50),
        _inv(caller="job-mid", cost_usd=0.10),
    ]
    data = aggregate_invocations(events, NOW)
    assert [c.caller for c in data.by_job_cost] == [
        "job-pricey",
        "job-mid",
        "job-cheap",
    ]
