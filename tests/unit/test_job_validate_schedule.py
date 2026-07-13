"""Tests for the timezone-correct, enriched POST /api/jobs/validate-schedule."""

import pytest

pytest.importorskip("croniter")

from fastapi.testclient import TestClient

import main as app_mod


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


@pytest.fixture
def client():
    with TestClient(app_mod.app) as c:
        yield c


def test_valid_returns_human_timezone_and_runs(client, monkeypatch):
    monkeypatch.delenv("JOB_TIMEZONE", raising=False)
    monkeypatch.delenv("CRON_TIMEZONE", raising=False)
    resp = client.post("/api/jobs/validate-schedule", json={"schedule": "0 9 * * *"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["human"] == "at 09:00"
    assert data["timezone"] == "UTC"
    assert len(data["next_runs"]) == 3
    # Preformatted local strings, ending in the 09:00 fire time.
    for run in data["next_runs"]:
        assert run.endswith("09:00")


def test_timezone_reflected_in_runs(client, monkeypatch):
    """With JOB_TIMEZONE=Europe/Paris the runs are formatted in Paris time."""
    monkeypatch.delenv("CRON_TIMEZONE", raising=False)
    monkeypatch.setenv("JOB_TIMEZONE", "Europe/Paris")
    resp = client.post("/api/jobs/validate-schedule", json={"schedule": "0 9 * * *"})
    data = resp.json()
    assert data["valid"] is True
    assert data["timezone"] == "Europe/Paris"
    # 09:00 Paris is not 09:00 UTC — the preview shows the Paris fire time.
    for run in data["next_runs"]:
        assert run.endswith("09:00")


def test_request_timezone_overrides_default(client, monkeypatch):
    """A timezone in the request body is used and echoed back."""
    monkeypatch.delenv("JOB_TIMEZONE", raising=False)
    monkeypatch.delenv("CRON_TIMEZONE", raising=False)
    resp = client.post(
        "/api/jobs/validate-schedule",
        json={"schedule": "0 9 * * *", "timezone": "America/New_York"},
    )
    data = resp.json()
    assert data["valid"] is True
    assert data["timezone"] == "America/New_York"
    for run in data["next_runs"]:
        assert run.endswith("09:00")


def test_invalid_request_timezone_falls_back(client, monkeypatch):
    """An invalid timezone in the request falls back to the server default."""
    monkeypatch.delenv("JOB_TIMEZONE", raising=False)
    monkeypatch.delenv("CRON_TIMEZONE", raising=False)
    resp = client.post(
        "/api/jobs/validate-schedule",
        json={"schedule": "0 9 * * *", "timezone": "Not/AZone"},
    )
    data = resp.json()
    assert data["valid"] is True
    assert data["timezone"] == "UTC"


def test_invalid_schedule(client):
    resp = client.post("/api/jobs/validate-schedule", json={"schedule": "not a cron"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert "error" in data
