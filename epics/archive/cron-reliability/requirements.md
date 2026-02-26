# Cron Reliability — Requirements

## Goal

Make cron job scheduling rock-solid by fixing three systemic issues: restart floods (all jobs fire at once after bot restart), double dispatch (same job runs twice in the same minute), and sequential bottleneck (jobs block each other). After this epic, cron jobs should fire exactly once at their scheduled time, run in parallel, and gracefully handle restarts.

## Context

The current cron system (built in the `cron-jobs` epic) works correctly ~95% of the time. But three edge cases cause recurring problems:

1. **Restart floods**: When `merlin.py` restarts, `is_job_due()` sees every job as overdue and fires them all immediately. This causes duplicate digests, reflections, changelogs, etc. at random times of day.
2. **Double dispatch**: Two `cron_runner.py` processes can overlap (e.g., when the scheduler ticks while a previous dispatcher is still running), and both decide the same job is due, running it twice.
3. **Sequential bottleneck**: The dispatcher runs jobs one after another. The 02:00 batch (6 jobs) takes ~8 minutes because each job waits for the previous one to finish, even though they're independent.

Root causes:
- `cron_state.py` uses a single shared `.state.json` with no file locking — concurrent read-modify-write causes lost updates
- `is_job_due()` has no staleness check — any missed schedule fires immediately regardless of how long ago it was
- `run_dispatcher()` runs jobs in a sequential `for` loop with no parallelism
- No concurrency guard prevents overlapping dispatcher instances

## Requirements

### R1: Per-job file locking
- **Status**: `accepted`
- Each job gets its own lock file: `cron-jobs/.locks/{job_id}.lock`
- Before executing a job, acquire an exclusive non-blocking `flock` on its lock file
- If the lock can't be acquired (another process/thread is running the same job), skip it with a warning log
- Lock is automatically released when the process/thread exits (kernel-level, survives crashes and SIGKILL)
- Manual execution (`--job`) also respects per-job locks to prevent overlap with the dispatcher
- The `.locks/` directory is gitignored

### R2: Per-job state files
- **Status**: `accepted`
- Replace the single shared `.state.json` with individual state files: `cron-jobs/.state/{job_id}` (plain text ISO timestamp)
- `get_last_run(job_id)` reads from `cron-jobs/.state/{job_id}`
- `set_last_run(job_id, ts)` writes to `cron-jobs/.state/{job_id}`
- Eliminates concurrent write corruption — each job only reads/writes its own state file
- One-time migration: on first run, if `.state.json` exists and `.state/` doesn't, migrate entries to individual files and rename `.state.json` to `.state.json.migrated`
- The `.state/` directory is gitignored

### R3: Parallel job execution
- **Status**: `accepted`
- `run_dispatcher()` collects all due jobs, then executes them in parallel using `concurrent.futures.ThreadPoolExecutor`
- Each thread: acquire per-job lock → run job → update state → release lock
- Max workers capped at 6 (prevents resource exhaustion if many jobs are due simultaneously)
- Errors in one job don't affect others (already the case, but verify with parallel execution)
- The dispatcher waits for all jobs to complete before exiting

### R4: Staleness window
- **Status**: `accepted`
- Add a grace period check to `is_job_due()`: if `now - next_scheduled_time > GRACE_PERIOD`, the job missed its window
- Default grace period: 15 minutes (covers normal dispatch jitter of ~4 min while blocking restart floods at 30+ min)
- When a job is skipped due to staleness, advance its state to `now` (not to `next_run` — avoids infinite loop for `* * * * *` jobs) and log a warning
- Grace period can be overridden per job via optional `grace_minutes` field in the job JSON (for future weekly/monthly jobs that may need a larger window)

### R5: Never-seen guard
- **Status**: `accepted`
- When `get_last_run()` returns `None` (job has no state entry — brand new or state was lost), do NOT run the job immediately
- Instead, initialize state to `now` and return `is_due = False`
- The job will fire at its next naturally scheduled time
- Users can still run new jobs immediately via `cron_runner.py --job <id>`
- Log an info message when a job is initialized: "New job {id} registered, first run at {next_time}"

### R6: History file locking
- **Status**: `accepted`
- `.history.json` remains a single shared file (splitting per-job would require dashboard/cron_manage changes for little benefit)
- Add `flock` around history read-modify-write operations in `append_history()` to prevent corruption from concurrent threads or overlapping processes
- Use a dedicated lock file `cron-jobs/.locks/_history.lock` (not the history file itself, to avoid issues with truncation)

### R7: Disable test-chunk-thread
- **Status**: `accepted`
- The `test-chunk-thread` job (`* * * * *`) is a temporary test that's been running continuously since Feb 14, costing ~$57/day
- Disable it (set `enabled: false`) as part of this epic
- Clean up its excessive history entries

### R8: Test coverage
- **Status**: `accepted`
- Update existing tests in `test_cron_runner.py` and `test_cron_state.py` to cover the new behavior
- New unit tests required:
  - **Staleness**: job skipped when `now - next_run > grace_period`, state advanced to `now`
  - **Staleness boundary**: job fires when `now - next_run < grace_period`
  - **Never-seen**: new job (no state) is NOT run, state initialized to `now`
  - **Per-job state**: read/write individual state files, migration from `.state.json`
  - **Parallel dispatch**: multiple due jobs execute concurrently (mock invoke_claude, verify all called)
  - **Per-job locking**: concurrent dispatch of same job — second attempt skipped
  - **History locking**: concurrent history appends don't corrupt the file
- Integration test:
  - Create 3 test jobs (2 due, 1 not due), run dispatcher, verify exactly 2 execute in parallel
  - Simulate restart scenario: set state to hours ago, verify stale jobs are skipped
  - Simulate double dispatch: run two dispatchers concurrently, verify no job runs twice

## Out of Scope

- Per-job grace period config (R4 mentions the field but implementation is future work unless trivially easy)
- Replacing the scheduler architecture (APScheduler, systemd timers)
- Dashboard changes (the dashboard reads `.history.json` which stays the same format)
- Parallel execution of `--job` manual runs (these remain single-process)

## Migration

- **State migration**: automatic one-time migration from `.state.json` → `.state/{job_id}` files
- **No breaking changes**: job file format unchanged, history format unchanged, CLI unchanged
- **Backwards compatible**: if someone runs the old `cron_runner.py` accidentally, it just won't find `.state.json` and treats all jobs as new (never-seen guard prevents a flood)
