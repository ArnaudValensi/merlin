"""Tests for the /cron dashboard page — Phase 7."""

import json
from pathlib import Path

import pytest

pytest.importorskip("croniter")

from fastapi.testclient import TestClient

import main as app_mod


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    """Disable auth for all route tests."""
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


@pytest.fixture(autouse=True)
def _isolated_cron_dirs(tmp_path):
    """Patch cron.manage and cron.state to use temp directories."""
    from cron import manage as cron_manage
    from cron import state as cron_state

    orig = {
        "manage_dir": cron_manage.CRON_JOBS_DIR,
        "state_dir": cron_state.CRON_JOBS_DIR,
        "state_state_dir": cron_state.STATE_DIR,
        "state_locks_dir": cron_state.LOCKS_DIR,
        "state_history_file": cron_state.HISTORY_FILE,
    }

    cron_jobs = tmp_path / "cron-jobs"
    cron_jobs.mkdir()

    cron_manage.CRON_JOBS_DIR = cron_jobs
    cron_state.CRON_JOBS_DIR = cron_jobs
    cron_state.STATE_DIR = cron_jobs / ".state"
    cron_state.LOCKS_DIR = cron_jobs / ".locks"
    cron_state.HISTORY_FILE = cron_jobs / ".history.json"

    yield cron_jobs

    cron_manage.CRON_JOBS_DIR = orig["manage_dir"]
    cron_state.CRON_JOBS_DIR = orig["state_dir"]
    cron_state.STATE_DIR = orig["state_state_dir"]
    cron_state.LOCKS_DIR = orig["state_locks_dir"]
    cron_state.HISTORY_FILE = orig["state_history_file"]


@pytest.fixture
def client():
    """TestClient for the app."""
    with TestClient(app_mod.app) as c:
        yield c


def _create_job(cron_dir: Path, job_id: str = "test-job", **overrides) -> None:
    """Write a job JSON file to the temp cron-jobs dir."""
    data = {
        "description": "A test job",
        "schedule": "0 9 * * *",
        "prompt": "Run the report",
        "enabled": True,
        "report_mode": "always",
        "max_turns": 0,
        "ephemeral": True,
        "grace_minutes": 15,
        "discord_channel": None,
        "created_at": "2026-03-22T00:00:00+00:00",
        **overrides,
    }
    (cron_dir / f"{job_id}.json").write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCronPageRoute:
    """Test GET /cron returns the page."""

    def test_cron_page_returns_200(self, client):
        resp = client.get("/cron")
        assert resp.status_code == 200

    def test_cron_page_contains_heading(self, client):
        resp = client.get("/cron")
        assert "Cron Jobs" in resp.text

    def test_cron_page_html_content_type(self, client):
        resp = client.get("/cron")
        assert "text/html" in resp.headers.get("content-type", "")

    def test_cron_page_empty_state(self, client):
        resp = client.get("/cron")
        assert "No cron jobs configured" in resp.text

    def test_cron_page_shows_job(self, client, _isolated_cron_dirs):
        _create_job(_isolated_cron_dirs, "my-report", description="Daily report")
        resp = client.get("/cron")
        assert "my-report" in resp.text
        assert "Daily report" in resp.text

    def test_cron_page_shows_schedule_human(self, client, _isolated_cron_dirs):
        _create_job(_isolated_cron_dirs, "hourly-check", schedule="0 * * * *")
        resp = client.get("/cron")
        assert "every hour" in resp.text

    def test_cron_page_shows_multiple_jobs(self, client, _isolated_cron_dirs):
        _create_job(_isolated_cron_dirs, "job-alpha")
        _create_job(_isolated_cron_dirs, "job-beta")
        resp = client.get("/cron")
        assert "job-alpha" in resp.text
        assert "job-beta" in resp.text

    def test_cron_page_disabled_job_class(self, client, _isolated_cron_dirs):
        _create_job(_isolated_cron_dirs, "disabled-job", enabled=False)
        resp = client.get("/cron")
        assert "disabled" in resp.text

    def test_cron_page_has_create_button(self, client):
        resp = client.get("/cron")
        assert "+ New Job" in resp.text


class TestCronNavItem:
    """Test that the Cron nav item appears in the sidebar."""

    def test_nav_item_in_sidebar(self, client):
        resp = client.get("/cron")
        assert 'href="/cron"' in resp.text

    def test_nav_item_label(self, client):
        resp = client.get("/cron")
        assert "Cron" in resp.text

    def test_nav_item_present_on_other_pages(self, client):
        """Cron nav item should be on all pages (it's a core nav item)."""
        resp = client.get("/extensions")
        assert 'href="/cron"' in resp.text


class TestValidateSchedule:
    """Test POST /api/cron/validate-schedule."""

    def test_valid_schedule(self, client):
        resp = client.post(
            "/api/cron/validate-schedule",
            json={"schedule": "0 9 * * *"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert len(data["next_runs"]) == 3
        assert data["human"] == "at 09:00"
        assert "timezone" in data

    def test_invalid_schedule(self, client):
        resp = client.post(
            "/api/cron/validate-schedule",
            json={"schedule": "not a cron"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert "error" in data

    def test_empty_schedule(self, client):
        resp = client.post(
            "/api/cron/validate-schedule",
            json={"schedule": ""},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
