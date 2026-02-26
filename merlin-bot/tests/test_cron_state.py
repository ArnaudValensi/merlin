"""Tests for cron_state.py — state, history, and locking."""

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def temp_cron_dir(tmp_path):
    """Fixture that patches cron_state to use a temporary directory."""
    import cron_state

    original_state_dir = cron_state.STATE_DIR
    original_locks_dir = cron_state.LOCKS_DIR
    original_history = cron_state.HISTORY_FILE
    cron_state.STATE_DIR = tmp_path / ".state"
    cron_state.LOCKS_DIR = tmp_path / ".locks"
    cron_state.HISTORY_FILE = tmp_path / ".history.json"

    yield tmp_path

    cron_state.STATE_DIR = original_state_dir
    cron_state.LOCKS_DIR = original_locks_dir
    cron_state.HISTORY_FILE = original_history


# ---------------------------------------------------------------------------
# State (per-job files)
# ---------------------------------------------------------------------------


class TestState:
    """Tests for per-job state tracking."""

    def test_get_last_run_none(self, temp_cron_dir):
        """get_last_run returns None for unknown job."""
        from cron_state import get_last_run

        assert get_last_run("unknown-job") is None

    def test_set_and_get_last_run(self, temp_cron_dir):
        """set_last_run and get_last_run work correctly."""
        from cron_state import get_last_run, set_last_run

        ts = datetime(2026, 2, 5, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", ts)
        result = get_last_run("test-job")
        assert result == ts

    def test_set_last_run_defaults_to_now(self, temp_cron_dir):
        """set_last_run uses current time when no timestamp provided."""
        from cron_state import get_last_run, set_last_run

        before = datetime.now(tz=timezone.utc)
        set_last_run("test-job")
        after = datetime.now(tz=timezone.utc)

        result = get_last_run("test-job")
        assert before <= result <= after

    def test_per_job_isolation(self, temp_cron_dir):
        """Each job has its own state file — no cross-contamination."""
        from cron_state import get_last_run, set_last_run

        ts1 = datetime(2026, 2, 5, 9, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 2, 5, 10, 0, 0, tzinfo=timezone.utc)
        set_last_run("job1", ts1)
        set_last_run("job2", ts2)

        assert get_last_run("job1") == ts1
        assert get_last_run("job2") == ts2

    def test_state_file_is_plain_text(self, temp_cron_dir):
        """State files are plain ISO timestamps, not JSON."""
        from cron_state import STATE_DIR, set_last_run

        ts = datetime(2026, 2, 5, 9, 0, 0, tzinfo=timezone.utc)
        set_last_run("test-job", ts)

        content = (STATE_DIR / "test-job").read_text()
        assert content == ts.isoformat()

    def test_get_last_run_handles_corrupt_file(self, temp_cron_dir):
        """get_last_run returns None for corrupt state file."""
        from cron_state import STATE_DIR, get_last_run

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "bad-job").write_text("not a timestamp")
        assert get_last_run("bad-job") is None

    def test_get_last_run_handles_empty_file(self, temp_cron_dir):
        """get_last_run returns None for empty state file."""
        from cron_state import STATE_DIR, get_last_run

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "empty-job").write_text("")
        assert get_last_run("empty-job") is None


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


class TestJobLocking:
    """Tests for per-job flock-based locking."""

    def test_acquire_and_release_lock(self, temp_cron_dir):
        """Can acquire and release a job lock."""
        from cron_state import acquire_job_lock, release_job_lock

        lock = acquire_job_lock("test-job")
        assert lock is not None
        release_job_lock(lock)

    def test_lock_prevents_second_acquisition(self, temp_cron_dir):
        """Second acquire attempt on same job returns None while locked."""
        from cron_state import acquire_job_lock, release_job_lock

        lock1 = acquire_job_lock("test-job")
        assert lock1 is not None

        lock2 = acquire_job_lock("test-job")
        assert lock2 is None  # Already locked

        release_job_lock(lock1)

        # Now should be available again
        lock3 = acquire_job_lock("test-job")
        assert lock3 is not None
        release_job_lock(lock3)

    def test_different_jobs_lock_independently(self, temp_cron_dir):
        """Different jobs can be locked simultaneously."""
        from cron_state import acquire_job_lock, release_job_lock

        lock1 = acquire_job_lock("job1")
        lock2 = acquire_job_lock("job2")
        assert lock1 is not None
        assert lock2 is not None

        release_job_lock(lock1)
        release_job_lock(lock2)

    def test_lock_blocks_across_threads(self, temp_cron_dir):
        """Lock prevents same job from running in two threads."""
        from cron_state import acquire_job_lock, release_job_lock

        results = {"thread_got_lock": False}
        lock1 = acquire_job_lock("shared-job")
        assert lock1 is not None

        def try_lock():
            lock2 = acquire_job_lock("shared-job")
            results["thread_got_lock"] = lock2 is not None
            if lock2:
                release_job_lock(lock2)

        t = threading.Thread(target=try_lock)
        t.start()
        t.join(timeout=2)

        assert results["thread_got_lock"] is False
        release_job_lock(lock1)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class TestHistory:
    """Tests for history (run log) tracking."""

    def test_read_history_empty(self, temp_cron_dir):
        """read_history returns empty dict when no history file exists."""
        from cron_state import read_history

        assert read_history() == {}

    def test_append_history_creates_entry(self, temp_cron_dir):
        """append_history creates a new entry for a job."""
        from cron_state import append_history, read_history

        ts = datetime(2026, 2, 5, 9, 0, 0, tzinfo=timezone.utc)
        append_history("test-job", exit_code=0, duration=1.5, session_id="abc", timestamp=ts)

        history = read_history()
        assert "test-job" in history
        assert len(history["test-job"]) == 1
        assert history["test-job"][0]["exit_code"] == 0
        assert history["test-job"][0]["duration"] == 1.5
        assert history["test-job"][0]["session_id"] == "abc"

    def test_append_history_multiple_entries(self, temp_cron_dir):
        """append_history accumulates entries."""
        from cron_state import append_history, get_history

        for i in range(5):
            append_history("test-job", exit_code=i, duration=float(i))

        runs = get_history("test-job")
        assert len(runs) == 5
        # Most recent first
        assert runs[0]["exit_code"] == 4
        assert runs[4]["exit_code"] == 0

    def test_history_rolling_limit(self, temp_cron_dir):
        """append_history enforces rolling limit of 100."""
        from cron_state import HISTORY_LIMIT, append_history, get_history

        for i in range(150):
            append_history("test-job", exit_code=i, duration=0.1)

        runs = get_history("test-job")
        assert len(runs) == HISTORY_LIMIT
        # Should have the most recent 100 (50-149)
        assert runs[0]["exit_code"] == 149
        assert runs[-1]["exit_code"] == 50

    def test_get_history_with_limit(self, temp_cron_dir):
        """get_history respects limit parameter."""
        from cron_state import append_history, get_history

        for i in range(20):
            append_history("test-job", exit_code=i, duration=0.1)

        runs = get_history("test-job", limit=5)
        assert len(runs) == 5

    def test_get_history_unknown_job(self, temp_cron_dir):
        """get_history returns empty list for unknown job."""
        from cron_state import get_history

        assert get_history("unknown-job") == []

    def test_get_all_history(self, temp_cron_dir):
        """get_all_history returns history for all jobs."""
        from cron_state import append_history, get_all_history

        append_history("job1", exit_code=0, duration=1.0)
        append_history("job2", exit_code=1, duration=2.0)
        append_history("job1", exit_code=0, duration=1.5)

        all_history = get_all_history()
        assert "job1" in all_history
        assert "job2" in all_history
        assert len(all_history["job1"]) == 2
        assert len(all_history["job2"]) == 1

    def test_read_history_handles_invalid_json(self, temp_cron_dir):
        """read_history returns empty dict for invalid JSON."""
        import cron_state

        cron_state.HISTORY_FILE.write_text("not valid json")
        from cron_state import read_history

        assert read_history() == {}

    def test_concurrent_history_appends(self, temp_cron_dir):
        """Multiple threads appending to history don't lose data."""
        from cron_state import append_history, read_history

        num_threads = 10
        entries_per_thread = 5
        errors = []

        def append_entries(job_id):
            try:
                for i in range(entries_per_thread):
                    append_history(job_id, exit_code=0, duration=float(i))
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=append_entries, args=(f"job-{i}",))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Errors during concurrent append: {errors}"

        history = read_history()
        total_entries = sum(len(v) for v in history.values())
        assert total_entries == num_threads * entries_per_thread
