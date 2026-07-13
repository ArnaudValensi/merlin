"""Tests for job execution log storage — Phase 4."""

import json

import pytest

pytest.importorskip("croniter")

from fastapi.testclient import TestClient

import main as app_mod
from job import logs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    """Disable auth for all route tests."""
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


@pytest.fixture(autouse=True)
def _isolated_job_dirs(tmp_path):
    """Patch job.manage, job.state, and job.logs to use temp directories."""
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
def log_dir(tmp_path):
    """Provide a temp job-logs directory and patch logs module to use it."""
    d = tmp_path / "job-logs"
    d.mkdir()

    original = logs._logs_base_dir

    def _patched():
        return d

    logs._logs_base_dir = _patched
    yield d
    logs._logs_base_dir = original


@pytest.fixture
def client():
    """TestClient for the app."""
    with TestClient(app_mod.app) as c:
        yield c


def _sample_entry(**overrides) -> dict:
    """Build a sample log entry."""
    data = {
        "job_id": "daily-digest",
        "timestamp": "2026-03-22T02:30:00+00:00",
        "exit_code": 0,
        "duration_seconds": 45.2,
        "cost_usd": 0.03,
        "session_id": "abc-123",
        "output": "The full Claude output...",
    }
    data.update(overrides)
    return data


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
# write_log
# ---------------------------------------------------------------------------


class TestWriteLog:
    def test_creates_file_at_correct_path(self, log_dir):
        """write_log creates file at {log_dir}/{job_id}/{timestamp}.json."""
        entry = _sample_entry()
        path = logs.write_log("daily-digest", entry)

        assert path.exists()
        assert path.parent == log_dir / "daily-digest"
        # Filename uses filesystem-safe timestamp
        assert path.name == "2026-03-22T02-30-00+00-00.json"

    def test_creates_directory_automatically(self, log_dir):
        """write_log creates the job directory if it doesn't exist."""
        entry = _sample_entry(job_id="new-job", timestamp="2026-01-01T00:00:00+00:00")
        path = logs.write_log("new-job", entry)

        assert path.exists()
        assert (log_dir / "new-job").is_dir()

    def test_file_content_matches(self, log_dir):
        """write_log writes correct JSON content."""
        entry = _sample_entry()
        path = logs.write_log("daily-digest", entry)

        data = json.loads(path.read_text())
        assert data["job_id"] == "daily-digest"
        assert data["exit_code"] == 0
        assert data["duration_seconds"] == 45.2
        assert data["output"] == "The full Claude output..."
        assert data["output_truncated"] is False

    def test_output_truncation(self, log_dir):
        """Output > 100KB is truncated with output_truncated flag."""
        big_output = "x" * 200_000  # 200KB
        entry = _sample_entry(output=big_output)
        path = logs.write_log("daily-digest", entry)

        data = json.loads(path.read_text())
        assert data["output_truncated"] is True
        # Output should be truncated to ~100KB
        assert len(data["output"].encode("utf-8")) <= 102400


# ---------------------------------------------------------------------------
# list_logs
# ---------------------------------------------------------------------------


class TestListLogs:
    def test_returns_sorted_newest_first(self, log_dir):
        """list_logs returns logs sorted by timestamp, newest first."""
        logs.write_log(
            "test-job",
            _sample_entry(
                job_id="test-job",
                timestamp="2026-03-20T10:00:00+00:00",
            ),
        )
        logs.write_log(
            "test-job",
            _sample_entry(
                job_id="test-job",
                timestamp="2026-03-22T10:00:00+00:00",
            ),
        )
        logs.write_log(
            "test-job",
            _sample_entry(
                job_id="test-job",
                timestamp="2026-03-21T10:00:00+00:00",
            ),
        )

        result = logs.list_logs("test-job")
        assert len(result) == 3
        # Newest first
        assert result[0]["timestamp"] == "2026-03-22T10:00:00+00:00"
        assert result[1]["timestamp"] == "2026-03-21T10:00:00+00:00"
        assert result[2]["timestamp"] == "2026-03-20T10:00:00+00:00"

    def test_with_limit(self, log_dir):
        """list_logs respects the limit parameter."""
        for i in range(5):
            logs.write_log(
                "test-job",
                _sample_entry(
                    job_id="test-job",
                    timestamp=f"2026-03-{20 + i:02d}T10:00:00+00:00",
                ),
            )

        result = logs.list_logs("test-job", limit=2)
        assert len(result) == 2

    def test_nonexistent_job_returns_empty(self, log_dir):
        """list_logs for nonexistent job returns empty list."""
        result = logs.list_logs("nonexistent-job")
        assert result == []

    def test_no_output_field(self, log_dir):
        """list_logs strips the output field from entries."""
        logs.write_log("test-job", _sample_entry(job_id="test-job"))
        result = logs.list_logs("test-job")
        assert len(result) == 1
        assert "output" not in result[0]
        # But other fields remain
        assert "job_id" in result[0]
        assert "exit_code" in result[0]


# ---------------------------------------------------------------------------
# read_log
# ---------------------------------------------------------------------------


class TestReadLog:
    def test_returns_full_content(self, log_dir):
        """read_log returns the full log entry including output."""
        entry = _sample_entry()
        logs.write_log("daily-digest", entry)

        result = logs.read_log("daily-digest", "2026-03-22T02:30:00+00:00")
        assert result is not None
        assert result["output"] == "The full Claude output..."
        assert result["job_id"] == "daily-digest"
        assert result["exit_code"] == 0

    def test_nonexistent_returns_none(self, log_dir):
        """read_log for nonexistent timestamp returns None."""
        result = logs.read_log("daily-digest", "2026-01-01T00:00:00+00:00")
        assert result is None

    def test_nonexistent_job_returns_none(self, log_dir):
        """read_log for nonexistent job returns None."""
        result = logs.read_log("nonexistent", "2026-01-01T00:00:00+00:00")
        assert result is None


# ---------------------------------------------------------------------------
# cleanup_logs
# ---------------------------------------------------------------------------


class TestCleanupLogs:
    def test_deletes_oldest_when_over_limit(self, log_dir):
        """cleanup_logs with 8 logs and max_logs=5 keeps only 5."""
        for i in range(8):
            logs.write_log(
                "test-job",
                _sample_entry(
                    job_id="test-job",
                    timestamp=f"2026-03-{10 + i:02d}T10:00:00+00:00",
                ),
            )

        deleted = logs.cleanup_logs("test-job", max_logs=5)
        assert deleted == 3

        # 5 remain
        remaining = list((log_dir / "test-job").glob("*.json"))
        assert len(remaining) == 5

        # Oldest 3 are gone
        assert not (log_dir / "test-job" / "2026-03-10T10-00-00+00-00.json").exists()
        assert not (log_dir / "test-job" / "2026-03-11T10-00-00+00-00.json").exists()
        assert not (log_dir / "test-job" / "2026-03-12T10-00-00+00-00.json").exists()

        # Newest 5 remain
        assert (log_dir / "test-job" / "2026-03-13T10-00-00+00-00.json").exists()
        assert (log_dir / "test-job" / "2026-03-17T10-00-00+00-00.json").exists()

    def test_no_deletion_when_under_limit(self, log_dir):
        """cleanup_logs does nothing when count <= max."""
        for i in range(3):
            logs.write_log(
                "test-job",
                _sample_entry(
                    job_id="test-job",
                    timestamp=f"2026-03-{10 + i:02d}T10:00:00+00:00",
                ),
            )

        deleted = logs.cleanup_logs("test-job", max_logs=5)
        assert deleted == 0

        remaining = list((log_dir / "test-job").glob("*.json"))
        assert len(remaining) == 3

    def test_nonexistent_job(self, log_dir):
        """cleanup_logs for nonexistent job returns 0."""
        deleted = logs.cleanup_logs("nonexistent", max_logs=5)
        assert deleted == 0


# ---------------------------------------------------------------------------
# delete_logs
# ---------------------------------------------------------------------------


class TestDeleteLogs:
    def test_removes_directory(self, log_dir):
        """delete_logs removes entire job log directory."""
        logs.write_log("test-job", _sample_entry(job_id="test-job"))
        assert (log_dir / "test-job").exists()

        logs.delete_logs("test-job")
        assert not (log_dir / "test-job").exists()

    def test_nonexistent_no_crash(self, log_dir):
        """delete_logs for nonexistent job does not raise."""
        logs.delete_logs("nonexistent")  # Should not raise


# ---------------------------------------------------------------------------
# Timestamp filename round-trip
# ---------------------------------------------------------------------------


class TestTimestampConversion:
    def test_round_trip(self):
        """Timestamp -> filename -> timestamp preserves original value."""
        ts = "2026-03-22T02:30:00+00:00"
        filename = logs._timestamp_to_filename(ts)
        assert filename == "2026-03-22T02-30-00+00-00.json"
        restored = logs._filename_to_timestamp(filename)
        assert restored == ts

    def test_utc_z_suffix(self):
        """Handles UTC 'Z' suffix timestamps."""
        ts = "2026-03-22T02:30:00Z"
        filename = logs._timestamp_to_filename(ts)
        assert ":" not in filename
        restored = logs._filename_to_timestamp(filename)
        assert restored == ts


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


class TestLogEndpoints:
    def test_get_logs_returns_list(self, client, log_dir):
        """GET /api/jobs/jobs/{id}/logs returns log list."""
        # Create a job first
        client.post("/api/jobs/jobs", json=_sample_job())

        # Write some logs
        logs.write_log(
            "test-job",
            _sample_entry(
                job_id="test-job",
                timestamp="2026-03-20T10:00:00+00:00",
            ),
        )
        logs.write_log(
            "test-job",
            _sample_entry(
                job_id="test-job",
                timestamp="2026-03-21T10:00:00+00:00",
            ),
        )

        resp = client.get("/api/jobs/jobs/test-job/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Newest first
        assert data[0]["timestamp"] == "2026-03-21T10:00:00+00:00"
        # No output field in list view
        assert "output" not in data[0]

    def test_get_logs_nonexistent_job_returns_404(self, client):
        """GET logs for nonexistent job returns 404."""
        resp = client.get("/api/jobs/jobs/nonexistent/logs")
        assert resp.status_code == 404

    def test_get_single_log_returns_content(self, client, log_dir):
        """GET /api/jobs/jobs/{id}/logs/{ts} returns full log with output."""
        client.post("/api/jobs/jobs", json=_sample_job())

        logs.write_log(
            "test-job",
            _sample_entry(
                job_id="test-job",
                timestamp="2026-03-22T02:30:00+00:00",
                output="Hello from Claude",
            ),
        )

        resp = client.get("/api/jobs/jobs/test-job/logs/2026-03-22T02:30:00+00:00")
        assert resp.status_code == 200
        data = resp.json()
        assert data["output"] == "Hello from Claude"
        assert data["job_id"] == "test-job"

    def test_get_single_log_not_found_returns_404(self, client):
        """GET nonexistent log returns 404."""
        client.post("/api/jobs/jobs", json=_sample_job())
        resp = client.get("/api/jobs/jobs/test-job/logs/2099-01-01T00:00:00+00:00")
        assert resp.status_code == 404

    def test_delete_job_also_deletes_logs(self, client, log_dir):
        """DELETE /api/jobs/jobs/{id} also removes execution logs."""
        client.post("/api/jobs/jobs", json=_sample_job())

        # Write a log
        logs.write_log(
            "test-job",
            _sample_entry(
                job_id="test-job",
                timestamp="2026-03-22T02:30:00+00:00",
            ),
        )
        assert (log_dir / "test-job").exists()

        resp = client.delete("/api/jobs/jobs/test-job")
        assert resp.status_code == 204

        # Log directory should be gone
        assert not (log_dir / "test-job").exists()
