"""Tests for the /jobs dashboard page — Phase 7."""

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
def _isolated_job_dirs(tmp_path):
    """Patch job.manage and job.state to use temp directories."""
    from job import manage as job_manage
    from job import state as job_state

    orig = {
        "manage_dir": job_manage.JOBS_DIR,
        "state_dir": job_state.JOBS_DIR,
        "state_state_dir": job_state.STATE_DIR,
        "state_locks_dir": job_state.LOCKS_DIR,
        "state_history_file": job_state.HISTORY_FILE,
    }

    jobs_dir_tmp = tmp_path / "jobs"
    jobs_dir_tmp.mkdir()

    job_manage.JOBS_DIR = jobs_dir_tmp
    job_state.JOBS_DIR = jobs_dir_tmp
    job_state.STATE_DIR = jobs_dir_tmp / ".state"
    job_state.LOCKS_DIR = jobs_dir_tmp / ".locks"
    job_state.HISTORY_FILE = jobs_dir_tmp / ".history.json"

    yield jobs_dir_tmp

    job_manage.JOBS_DIR = orig["manage_dir"]
    job_state.JOBS_DIR = orig["state_dir"]
    job_state.STATE_DIR = orig["state_state_dir"]
    job_state.LOCKS_DIR = orig["state_locks_dir"]
    job_state.HISTORY_FILE = orig["state_history_file"]


@pytest.fixture
def client():
    """TestClient for the app."""
    with TestClient(app_mod.app) as c:
        yield c


def _create_job(jobs_dir: Path, job_id: str = "test-job", **overrides) -> None:
    """Write a job JSON file to the temp jobs dir."""
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
    (jobs_dir / f"{job_id}.json").write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestJobPageRoute:
    """Test GET /jobs returns the page."""

    def test_jobs_page_returns_200(self, client):
        resp = client.get("/jobs")
        assert resp.status_code == 200

    def test_jobs_page_contains_heading(self, client):
        resp = client.get("/jobs")
        assert "Jobs" in resp.text

    def test_jobs_page_html_content_type(self, client):
        resp = client.get("/jobs")
        assert "text/html" in resp.headers.get("content-type", "")

    def test_jobs_page_empty_state(self, client):
        resp = client.get("/jobs")
        assert "No jobs configured" in resp.text

    def test_jobs_page_shows_job(self, client, _isolated_job_dirs):
        _create_job(_isolated_job_dirs, "my-report", description="Daily report")
        resp = client.get("/jobs")
        assert "my-report" in resp.text
        assert "Daily report" in resp.text

    def test_jobs_page_shows_schedule_human(self, client, _isolated_job_dirs):
        _create_job(_isolated_job_dirs, "hourly-check", schedule="0 * * * *")
        resp = client.get("/jobs")
        assert "every hour" in resp.text

    def test_jobs_page_shows_multiple_jobs(self, client, _isolated_job_dirs):
        _create_job(_isolated_job_dirs, "job-alpha")
        _create_job(_isolated_job_dirs, "job-beta")
        resp = client.get("/jobs")
        assert "job-alpha" in resp.text
        assert "job-beta" in resp.text

    def test_jobs_page_disabled_job_class(self, client, _isolated_job_dirs):
        _create_job(_isolated_job_dirs, "disabled-job", enabled=False)
        resp = client.get("/jobs")
        assert "disabled" in resp.text

    def test_jobs_page_has_create_button(self, client):
        resp = client.get("/jobs")
        assert "+ New Job" in resp.text


class TestJobNavItem:
    """Test that the Job nav item appears in the sidebar."""

    def test_nav_item_in_sidebar(self, client):
        resp = client.get("/jobs")
        assert 'href="/jobs"' in resp.text

    def test_nav_item_label(self, client):
        resp = client.get("/jobs")
        assert "Jobs" in resp.text

    def test_nav_item_present_on_other_pages(self, client):
        """Job nav item should be on all pages (it's a core nav item)."""
        resp = client.get("/extensions")
        assert 'href="/jobs"' in resp.text


class TestValidateSchedule:
    """Test POST /api/job/validate-schedule."""

    def test_valid_schedule(self, client):
        resp = client.post(
            "/api/job/validate-schedule",
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
            "/api/job/validate-schedule",
            json={"schedule": "not a cron"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert "error" in data

    def test_empty_schedule(self, client):
        resp = client.post(
            "/api/job/validate-schedule",
            json={"schedule": ""},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
