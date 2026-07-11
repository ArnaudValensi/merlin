"""Tests for the job REST API endpoints — Phase 3."""

from unittest.mock import AsyncMock, patch

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


def _sample_job(**overrides) -> dict:
    """Build a sample job creation payload."""
    data = {
        "id": "test-job",
        "schedule": "0 9 * * *",
        "prompt": "Run the daily report",
        "description": "Daily report job",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# POST /api/job/jobs — create
# ---------------------------------------------------------------------------


class TestCreateJob:
    def test_create_returns_201(self, client):
        """POST /api/job/jobs creates a job and returns 201."""
        resp = client.post("/api/job/jobs", json=_sample_job())
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "test-job"
        assert data["schedule"] == "0 9 * * *"
        assert data["prompt"] == "Run the daily report"
        assert "created_at" in data
        assert "next_run" in data

    def test_create_duplicate_returns_409(self, client):
        """POST duplicate id returns 409."""
        client.post("/api/job/jobs", json=_sample_job())
        resp = client.post("/api/job/jobs", json=_sample_job())
        assert resp.status_code == 409

    def test_create_invalid_schedule_returns_422(self, client):
        """POST with invalid cron schedule returns 422."""
        resp = client.post("/api/job/jobs", json=_sample_job(schedule="invalid"))
        assert resp.status_code == 422

    def test_create_missing_prompt_returns_422(self, client):
        """POST without prompt returns 422."""
        data = _sample_job()
        del data["prompt"]
        resp = client.post("/api/job/jobs", json=data)
        assert resp.status_code == 422

    def test_create_empty_prompt_returns_422(self, client):
        """POST with empty prompt returns 422."""
        resp = client.post("/api/job/jobs", json=_sample_job(prompt=""))
        assert resp.status_code == 422

    def test_create_invalid_id_uppercase(self, client):
        """POST with uppercase id returns 422."""
        resp = client.post("/api/job/jobs", json=_sample_job(id="MyJob"))
        assert resp.status_code == 422

    def test_create_without_schedule(self, client, _isolated_job_dirs):
        """POST without a schedule creates a job with no schedule trigger."""
        data = _sample_job()
        del data["schedule"]
        resp = client.post("/api/job/jobs", json=data)
        assert resp.status_code == 201
        assert resp.json()["next_run"] is None
        import json as json_mod

        stored = json_mod.loads((_isolated_job_dirs / "test-job.json").read_text())
        assert "schedule" not in stored

    def test_create_with_webhook_block(self, client, _isolated_job_dirs):
        """POST with a webhook block persists it in the job file."""
        resp = client.post(
            "/api/job/jobs", json=_sample_job(webhook={"secret": "whk_test"})
        )
        assert resp.status_code == 201
        import json as json_mod

        stored = json_mod.loads((_isolated_job_dirs / "test-job.json").read_text())
        assert stored["webhook"] == {"secret": "whk_test"}

    def test_create_webhook_without_secret_returns_422(self, client):
        resp = client.post("/api/job/jobs", json=_sample_job(webhook={}))
        assert resp.status_code == 422

    def test_create_invalid_id_double_hyphen(self, client):
        """POST with -- in id returns 422."""
        resp = client.post("/api/job/jobs", json=_sample_job(id="my--job"))
        assert resp.status_code == 422

    def test_create_invalid_id_too_long(self, client):
        """POST with id > 30 chars returns 422."""
        long_id = "a" * 31
        resp = client.post("/api/job/jobs", json=_sample_job(id=long_id))
        assert resp.status_code == 422

    def test_create_invalid_id_starts_with_digit(self, client):
        """POST with id starting with digit returns 422."""
        resp = client.post("/api/job/jobs", json=_sample_job(id="1job"))
        assert resp.status_code == 422

    def test_create_with_all_fields(self, client):
        """POST with all optional fields set correctly."""
        resp = client.post(
            "/api/job/jobs",
            json=_sample_job(
                enabled=False,
                report_mode="silent",
                max_turns=5,
                ephemeral=False,
                grace_minutes=30,
                discord_channel="123456",
            ),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["enabled"] is False
        assert data["report_mode"] == "silent"
        assert data["max_turns"] == 5
        assert data["discord_channel"] == "123456"

    def test_create_invalid_report_mode(self, client):
        """POST with invalid report_mode returns 422."""
        resp = client.post("/api/job/jobs", json=_sample_job(report_mode="verbose"))
        assert resp.status_code == 422

    def test_create_command_job_persists_fields(self, client):
        """POST a command job persists type/command/working_dir."""
        resp = client.post(
            "/api/job/jobs",
            json={
                "id": "backup-job",
                "schedule": "0 3 * * *",
                "type": "command",
                "command": "echo hi",
                "working_dir": "/tmp/work",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["type"] == "command"
        assert data["command"] == "echo hi"
        assert data["working_dir"] == "/tmp/work"

    def test_create_command_job_without_command_returns_422(self, client):
        """POST a command job without a command returns 422."""
        resp = client.post(
            "/api/job/jobs",
            json={"id": "bad-cmd", "schedule": "0 3 * * *", "type": "command"},
        )
        assert resp.status_code == 422

    def test_create_command_job_allows_empty_prompt(self, client):
        """A command job does not need a prompt."""
        resp = client.post(
            "/api/job/jobs",
            json={
                "id": "cmd-only",
                "schedule": "0 3 * * *",
                "type": "command",
                "command": "ls",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["type"] == "command"

    def test_create_default_type_is_prompt(self, client):
        """A job created without a type defaults to prompt."""
        resp = client.post("/api/job/jobs", json=_sample_job())
        assert resp.status_code == 201
        assert resp.json()["type"] == "prompt"

    def test_create_persists_timezone(self, client):
        """POST with a timezone persists it."""
        resp = client.post("/api/job/jobs", json=_sample_job(timezone="Europe/Paris"))
        assert resp.status_code == 201
        assert resp.json()["timezone"] == "Europe/Paris"

    def test_create_invalid_timezone_returns_422(self, client):
        """POST with an invalid timezone returns 422."""
        resp = client.post("/api/job/jobs", json=_sample_job(timezone="Not/AZone"))
        assert resp.status_code == 422

    def test_create_default_timezone_is_null(self, client):
        """A job created without a timezone stores null (server default)."""
        resp = client.post("/api/job/jobs", json=_sample_job())
        assert resp.status_code == 201
        assert resp.json()["timezone"] is None


# ---------------------------------------------------------------------------
# GET /api/job/jobs — list
# ---------------------------------------------------------------------------


class TestListJobs:
    def test_list_empty(self, client):
        """GET /api/job/jobs returns empty list when no jobs."""
        resp = client.get("/api/job/jobs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_all(self, client):
        """GET /api/job/jobs returns all created jobs."""
        client.post("/api/job/jobs", json=_sample_job(id="job-a"))
        client.post("/api/job/jobs", json=_sample_job(id="job-b"))
        resp = client.get("/api/job/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        ids = {j["id"] for j in data}
        assert ids == {"job-a", "job-b"}

    def test_list_includes_next_run(self, client):
        """Listed jobs include next_run field."""
        client.post("/api/job/jobs", json=_sample_job())
        resp = client.get("/api/job/jobs")
        data = resp.json()
        assert "next_run" in data[0]
        assert data[0]["next_run"] is not None


# ---------------------------------------------------------------------------
# GET /api/job/jobs/{job_id} — get single
# ---------------------------------------------------------------------------


class TestGetJob:
    def test_get_existing(self, client):
        """GET /api/job/jobs/{id} returns job with history."""
        client.post("/api/job/jobs", json=_sample_job())
        resp = client.get("/api/job/jobs/test-job")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "test-job"
        assert "history" in data
        assert isinstance(data["history"], list)

    def test_get_nonexistent_returns_404(self, client):
        """GET /api/job/jobs/missing returns 404."""
        resp = client.get("/api/job/jobs/missing")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/job/jobs/{job_id} — update
# ---------------------------------------------------------------------------


class TestUpdateJob:
    def test_update_fields(self, client):
        """PUT /api/job/jobs/{id} merges only provided fields."""
        client.post("/api/job/jobs", json=_sample_job())
        resp = client.put(
            "/api/job/jobs/test-job",
            json={
                "description": "Updated description",
                "schedule": "0 10 * * *",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "Updated description"
        assert data["schedule"] == "0 10 * * *"
        # Unchanged fields preserved
        assert data["prompt"] == "Run the daily report"

    def test_update_nonexistent_returns_404(self, client):
        """PUT nonexistent job returns 404."""
        resp = client.put("/api/job/jobs/missing", json={"description": "test"})
        assert resp.status_code == 404

    def test_update_empty_schedule_removes_trigger(self, client, _isolated_job_dirs):
        """PUT with schedule='' removes the schedule trigger from the job."""
        client.post("/api/job/jobs", json=_sample_job())
        resp = client.put("/api/job/jobs/test-job", json={"schedule": ""})
        assert resp.status_code == 200
        assert resp.json()["next_run"] is None
        import json as json_mod

        stored = json_mod.loads((_isolated_job_dirs / "test-job.json").read_text())
        assert "schedule" not in stored

    def test_update_clears_working_dir(self, client, _isolated_job_dirs):
        """PUT with working_dir='' clears it (was silently ignored before:
        exclude_none dropped a null and kept the old value)."""
        import json as json_mod

        client.post("/api/job/jobs", json=_sample_job(working_dir="/old/path"))
        assert (
            json_mod.loads((_isolated_job_dirs / "test-job.json").read_text())[
                "working_dir"
            ]
            == "/old/path"
        )
        resp = client.put("/api/job/jobs/test-job", json={"working_dir": ""})
        assert resp.status_code == 200
        stored = json_mod.loads((_isolated_job_dirs / "test-job.json").read_text())
        assert "working_dir" not in stored

    def test_create_empty_working_dir_stored_absent(self, client, _isolated_job_dirs):
        import json as json_mod

        client.post("/api/job/jobs", json=_sample_job(working_dir=""))
        stored = json_mod.loads((_isolated_job_dirs / "test-job.json").read_text())
        assert stored["working_dir"] is None

    def test_update_invalid_schedule(self, client):
        """PUT with invalid schedule returns 422."""
        client.post("/api/job/jobs", json=_sample_job())
        resp = client.put("/api/job/jobs/test-job", json={"schedule": "bad"})
        assert resp.status_code == 422

    def test_update_empty_prompt_rejected(self, client):
        """PUT with empty prompt returns 422."""
        client.post("/api/job/jobs", json=_sample_job())
        resp = client.put("/api/job/jobs/test-job", json={"prompt": ""})
        assert resp.status_code == 422

    def test_update_type_switch_without_command_rejected(self, client):
        """Switching a prompt job to type=command requires a command."""
        client.post("/api/job/jobs", json=_sample_job())
        resp = client.put("/api/job/jobs/test-job", json={"type": "command"})
        assert resp.status_code == 422
        assert "command" in resp.json()["detail"]

    def test_update_type_switch_with_command_accepted(self, client):
        """Switching to type=command works when a command is supplied."""
        client.post("/api/job/jobs", json=_sample_job())
        resp = client.put(
            "/api/job/jobs/test-job",
            json={"type": "command", "command": "echo hi"},
        )
        assert resp.status_code == 200
        assert resp.json()["type"] == "command"

    def test_update_type_switch_to_prompt_without_prompt_rejected(self, client):
        """Switching a command job to type=prompt requires a prompt."""
        client.post(
            "/api/job/jobs",
            json={
                "id": "cmd-only",
                "schedule": "0 3 * * *",
                "type": "command",
                "command": "echo hi",
            },
        )
        resp = client.put("/api/job/jobs/cmd-only", json={"type": "prompt"})
        assert resp.status_code == 422
        assert "prompt" in resp.json()["detail"]

    def test_update_command_field(self, client):
        """PUT updates a command job's command."""
        client.post(
            "/api/job/jobs",
            json={
                "id": "cmd-job",
                "schedule": "0 3 * * *",
                "type": "command",
                "command": "echo old",
            },
        )
        resp = client.put("/api/job/jobs/cmd-job", json={"command": "echo new"})
        assert resp.status_code == 200
        assert resp.json()["command"] == "echo new"


# ---------------------------------------------------------------------------
# DELETE /api/job/jobs/{job_id} — delete
# ---------------------------------------------------------------------------


class TestDeleteJob:
    def test_delete_returns_204(self, client):
        """DELETE /api/job/jobs/{id} returns 204."""
        client.post("/api/job/jobs", json=_sample_job())
        resp = client.delete("/api/job/jobs/test-job")
        assert resp.status_code == 204

        # Confirm gone
        resp2 = client.get("/api/job/jobs/test-job")
        assert resp2.status_code == 404

    def test_delete_nonexistent_returns_404(self, client):
        """DELETE nonexistent job returns 404."""
        resp = client.delete("/api/job/jobs/missing")
        assert resp.status_code == 404

    def test_delete_cleans_state(self, client, _isolated_job_dirs):
        """DELETE removes state file and lock file."""
        _isolated_job_dirs
        from job import state as job_state

        # Create job and add state/lock
        client.post("/api/job/jobs", json=_sample_job())
        job_state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (job_state.STATE_DIR / "test-job").write_text("2026-01-01T00:00:00+00:00")
        job_state.LOCKS_DIR.mkdir(parents=True, exist_ok=True)
        (job_state.LOCKS_DIR / "test-job.lock").write_text("")

        resp = client.delete("/api/job/jobs/test-job")
        assert resp.status_code == 204
        assert not (job_state.STATE_DIR / "test-job").exists()
        assert not (job_state.LOCKS_DIR / "test-job.lock").exists()


# ---------------------------------------------------------------------------
# POST /api/job/jobs/{job_id}/toggle — toggle enabled
# ---------------------------------------------------------------------------


class TestToggleJob:
    def test_toggle_flips_enabled(self, client):
        """POST /api/job/jobs/{id}/toggle flips enabled state."""
        client.post("/api/job/jobs", json=_sample_job(enabled=True))
        resp = client.post("/api/job/jobs/test-job/toggle")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        # Toggle back
        resp2 = client.post("/api/job/jobs/test-job/toggle")
        assert resp2.status_code == 200
        assert resp2.json()["enabled"] is True

    def test_toggle_nonexistent_returns_404(self, client):
        """POST toggle nonexistent job returns 404."""
        resp = client.post("/api/job/jobs/missing/toggle")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/job/jobs/{job_id}/run — trigger run
# ---------------------------------------------------------------------------


class TestRunJob:
    def test_run_returns_202(self, client):
        """POST /api/job/jobs/{id}/run triggers subprocess and returns 202."""
        client.post("/api/job/jobs", json=_sample_job())
        with patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock
        ) as mock_exec:
            resp = client.post("/api/job/jobs/test-job/run")
            assert resp.status_code == 202
            data = resp.json()
            assert data["ok"] is True
            mock_exec.assert_called_once()

    def test_run_nonexistent_returns_404(self, client):
        """POST run nonexistent job returns 404."""
        resp = client.post("/api/job/jobs/missing/run")
        assert resp.status_code == 404
