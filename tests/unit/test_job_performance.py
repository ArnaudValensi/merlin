"""Tests for GET /api/jobs/performance.

Auth note: every /api/jobs/* route is mounted under Depends(require_auth),
which in this codebase *redirects* unauthenticated requests to /login (303)
rather than returning a bare 401 (see main.py:_auth_redirect_handler). The
requirements doc phrased R9.1 as "returns 401"; we assert the real protective
behavior: the request is redirected to login and the performance JSON is not
served, which is what actually guards the endpoint here. Making /api/* return
401 app-wide is tracked as future work in docs/TODO.md.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("croniter")

from fastapi.testclient import TestClient

import auth
import main as app_mod
from lib import event_log


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    """Disable auth for all route tests (re-enabled explicitly where needed)."""
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


@pytest.fixture(autouse=True)
def _isolated_engine_log(tmp_path, monkeypatch):
    """Point the shared reader at a temp engine log for every test."""
    path = tmp_path / "engine-log.jsonl"
    monkeypatch.setattr(event_log, "ENGINE_LOG_PATH", path)
    return path


@pytest.fixture
def client():
    with TestClient(app_mod.app) as c:
        yield c


def _inv(
    caller: str, dt: datetime, *, duration=1.0, cost_usd=0.01, exit_code=0
) -> dict:
    return {
        "type": "invocation",
        "timestamp": dt.isoformat(),
        "caller": caller,
        "duration": duration,
        "cost_usd": cost_usd,
        "exit_code": exit_code,
    }


def _write(path, *events: dict) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_endpoint_requires_auth(client, monkeypatch):
    """Unauthenticated requests are redirected to login, not served the data."""
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
    auth.configure("secret")

    resp = client.get("/api/jobs/performance", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
    assert "/login" in resp.headers.get("location", "")


def test_endpoint_authenticated_returns_data(client):
    """With auth disabled (local mode) the endpoint serves data — the inverse
    of the redirect test, proving the dependency is the only gate."""
    resp = client.get("/api/jobs/performance")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Shape + filtering
# ---------------------------------------------------------------------------


def test_endpoint_returns_200_with_expected_shape(client, _isolated_engine_log):
    now = datetime.now(tz=timezone.utc)
    _write(_isolated_engine_log, _inv("job-foo", now - timedelta(hours=1)))

    resp = client.get("/api/jobs/performance")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {
        "timeseries",
        "success_rate",
        "by_job_duration",
        "by_job_cost",
    }
    assert data["success_rate"]["total"] == 1


def test_endpoint_filters_to_job_callers_only(client, _isolated_engine_log):
    now = datetime.now(tz=timezone.utc)
    _write(
        _isolated_engine_log,
        _inv("job-a", now - timedelta(hours=1)),
        _inv("job-b", now - timedelta(hours=2)),
        _inv("job-c", now - timedelta(hours=3)),
        _inv("discord", now - timedelta(hours=1)),
        _inv("discord", now - timedelta(hours=2)),
    )

    resp = client.get("/api/jobs/performance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success_rate"]["total"] == 3
    callers = {d["caller"] for d in data["by_job_duration"]}
    assert callers == {"job-a", "job-b", "job-c"}


def test_endpoint_default_since_is_7_days(client, _isolated_engine_log):
    now = datetime.now(tz=timezone.utc)
    _write(
        _isolated_engine_log,
        _inv("job-recent", now - timedelta(days=1)),
        _inv("job-old", now - timedelta(days=10)),
    )

    resp = client.get("/api/jobs/performance")  # no ?since
    data = resp.json()
    assert data["success_rate"]["total"] == 1
    assert {d["caller"] for d in data["by_job_duration"]} == {"job-recent"}


def test_endpoint_custom_since_is_respected(client, _isolated_engine_log):
    now = datetime.now(tz=timezone.utc)
    _write(
        _isolated_engine_log,
        _inv("job-very-recent", now - timedelta(hours=12)),
        _inv("job-two-days", now - timedelta(days=2)),
    )

    since = (now - timedelta(days=1)).isoformat()
    # params= percent-encodes the value (matching the frontend's
    # encodeURIComponent), so the "+00:00" offset is not mangled into a space.
    resp = client.get("/api/jobs/performance", params={"since": since})
    data = resp.json()
    assert data["success_rate"]["total"] == 1
    assert {d["caller"] for d in data["by_job_duration"]} == {"job-very-recent"}


def test_endpoint_empty_engine_log_returns_empty_perf_data(
    client, _isolated_engine_log
):
    # No file written — read_events sees a missing file.
    assert not _isolated_engine_log.exists()
    resp = client.get("/api/jobs/performance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success_rate"] == {"success": 0, "error": 0, "total": 0}
    assert data["timeseries"] == []
    assert data["by_job_duration"] == []
    assert data["by_job_cost"] == []


def test_endpoint_bad_since_returns_400(client, _isolated_engine_log):
    """A non-ISO 'since' is a client error, not a 500."""
    resp = client.get("/api/jobs/performance", params={"since": "not-a-timestamp"})
    assert resp.status_code == 400


def test_endpoint_naive_since_is_accepted(client, _isolated_engine_log):
    """A timezone-naive 'since' (no offset) is coerced to UTC, not a 500."""
    now = datetime.now(tz=timezone.utc)
    _write(_isolated_engine_log, _inv("job-foo", now - timedelta(hours=1)))

    naive = (now - timedelta(days=1)).replace(tzinfo=None).isoformat()
    resp = client.get("/api/jobs/performance", params={"since": naive})
    assert resp.status_code == 200
    assert resp.json()["success_rate"]["total"] == 1
