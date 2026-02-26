# Cron Reliability — Tasks

## T1: Per-job state files + migration
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: —
- **Description**: Refactor `cron_state.py` to use per-job state files instead of a single `.state.json`.
  - `get_last_run(job_id)` → reads `cron-jobs/.state/{job_id}` (plain text ISO timestamp)
  - `set_last_run(job_id, ts)` → writes `cron-jobs/.state/{job_id}`
  - Add migration function: if `.state.json` exists and `.state/` doesn't, split entries into individual files, rename `.state.json` to `.state.json.migrated`
  - Call migration at the top of `run_dispatcher()` and `run_single_job()`
  - Add `.state/` to `cron-jobs/.gitignore`
  - Remove old `read_state()` / `write_state()` functions (no longer needed)
- **Validation**: Unit tests for per-job read/write, migration from `.state.json`, corrupt file handling

## T2: Staleness window + never-seen guard
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T1
- **Description**: Modify `is_job_due()` in `cron_runner.py`:
  - If `get_last_run()` returns `None`: initialize state to `now`, log "New job {id} registered, first run at {next_time}", return `False`
  - If `now - next_run > GRACE_PERIOD` (default 15 min): advance state to `now`, log warning "Job {id} missed its window by {N} min, skipping", return `False`
  - Support optional `grace_minutes` field from job JSON (pass to `is_job_due` or read in dispatcher)
  - Add `DEFAULT_GRACE_MINUTES = 15` constant
- **Validation**: Unit tests for: never-seen guard, staleness skip, staleness boundary (just under grace → fires), grace period with different job schedules (`* * * * *`, daily, etc.)

## T3: Per-job file locking
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: —
- **Description**: Add per-job `flock` to prevent double dispatch:
  - Create `cron-jobs/.locks/` directory (gitignored)
  - Add `acquire_job_lock(job_id)` function that returns a file object (or None if already locked) using `fcntl.flock(fd, LOCK_EX | LOCK_NB)`
  - In `run_job()`: acquire lock at the start, release at the end (use try/finally)
  - If lock can't be acquired, log warning "Job {id} already running, skipping" and return early
  - Both `run_dispatcher()` and `run_single_job()` paths go through `run_job()`, so both are protected
- **Validation**: Unit test simulating concurrent lock acquisition (use multiprocessing or threading to verify second attempt is blocked)

## T4: History file locking
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T3
- **Description**: Add `flock` around history file writes in `append_history()`:
  - Use `cron-jobs/.locks/_history.lock` as the lock file
  - Acquire exclusive lock before read-modify-write cycle
  - Release after write completes
  - Ensure lock is released even on error (try/finally)
- **Validation**: Stress test — 10 concurrent threads each appending to history, verify no data loss or corruption

## T5: Parallel job execution
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T1, T2, T3, T4
- **Description**: Modify `run_dispatcher()` to execute due jobs in parallel:
  - Collect all due jobs first (checking `is_job_due` for each)
  - Submit to `concurrent.futures.ThreadPoolExecutor(max_workers=6)`
  - Each thread runs `run_job(job_id, job)` (which handles its own locking, state, history)
  - Wait for all futures with `as_completed()`, log results
  - Handle exceptions from individual threads without crashing the dispatcher
- **Validation**: Integration test — create 3 due jobs with mocked `invoke_claude`, verify all 3 start within 1 second of each other (not sequential)

## T6: Update existing tests
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T1, T2, T3, T4, T5
- **Description**: Update `test_cron_runner.py` and `test_cron_state.py`:
  - Fix `temp_cron_dir` fixture to use per-job state directory instead of `.state.json`
  - Update `TestIsJobDue.test_first_run_is_due` → now returns `False` (never-seen guard)
  - Add test: `test_first_run_initializes_state` — verifies state is set to `now`
  - Add test: `test_stale_job_skipped` — job >15 min past schedule → not due, state advanced
  - Add test: `test_stale_job_boundary` — job 14 min past schedule → still due
  - Add test: `test_every_minute_stale_after_long_outage` — `* * * * *` job 30 min stale → skipped
  - Add test: `test_parallel_dispatch` — 3 due jobs all execute (mock invoke_claude called 3 times)
  - Add test: `test_per_job_lock_prevents_double_run` — simulate overlapping run_job calls
  - Add test: `test_concurrent_history_append` — 10 threads append, verify all entries present
  - Add test: `test_state_migration` — `.state.json` with 3 entries → 3 individual files created
  - Verify all existing tests still pass (some will need adaptation for new state format)

## T7: Integration / smoke test
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T6
- **Description**: End-to-end validation of the complete system:
  - **Parallel execution**: Create 3 test jobs (all due), run dispatcher, measure wall-clock time. Should complete in time of slowest single job, not sum of all.
  - **Restart simulation**: Set all job states to 2 hours ago, run dispatcher, verify zero jobs fire (all stale). Then set states to 5 minutes ago for a daily-at-now job, verify it fires (within grace).
  - **Double dispatch prevention**: Spawn two `cron_runner.py` processes simultaneously (via subprocess), verify from history that each job ran exactly once.
  - **State migration**: Create a `.state.json` with test data, run dispatcher, verify `.state/` files exist and `.state.json.migrated` was created.
  - **Manual execution during dispatch**: Start dispatcher (with a slow mock job), simultaneously run `--job` for a different job, verify both complete without issues.
  - Document results in journal entry.

## T8: Disable test-chunk-thread + cleanup
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: —
- **Description**:
  - Set `enabled: false` in `cron-jobs/test-chunk-thread.json`
  - Trim its history entries in `.history.json` to the most recent 10 (it currently has 97+)
  - Verify the job no longer fires
