# Cron System

Reference documentation for the scheduled job system. Covers job files, dispatch, state tracking, locking, and the scheduler loop.

## Overview

Cron jobs are JSON files in `merlin-bot/cron-jobs/`. A built-in asyncio scheduler in `merlin_bot.py` spawns `cron_runner.py` every minute. The dispatcher loads all enabled jobs, checks which are due, and executes them in parallel via `ThreadPoolExecutor` with per-job file locks.

```
merlin_bot.py (_cron_scheduler)
  └─ spawns cron_runner.py every minute
       └─ loads cron-jobs/*.json
       └─ checks is_job_due() for each
       └─ ThreadPoolExecutor(max_workers=6) for due jobs
            └─ acquire_job_lock() → invoke_claude() → set_last_run() → append_history()
```

## Job File Format

**Location**: `merlin-bot/cron-jobs/{job-id}.json`

Job ID is the filename without `.json` (e.g., `daily-digest.json` → job ID `daily-digest`).

```json
{
  "description": "Human-readable summary",
  "schedule": "30 2 * * *",
  "prompt": "The prompt sent to Claude when the job runs",
  "channel": "YOUR_CHANNEL_ID",
  "enabled": true,
  "report_mode": "always",
  "max_turns": 0,
  "ephemeral": true,
  "grace_minutes": 15,
  "created_at": "2026-02-05T11:50:28.679794+00:00"
}
```

### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `schedule` | string | **required** | 5-field cron expression (via `croniter`) |
| `prompt` | string | **required** | Prompt sent to Claude |
| `channel` | string | **required** | Discord channel ID for output |
| `description` | string | `""` | Human-readable summary |
| `enabled` | boolean | `true` | Toggle without deleting |
| `report_mode` | string | `"always"` | `"always"` or `"silent"` — controls whether Claude posts to Discord |
| `max_turns` | integer | `0` | Max agentic turns (0 = unlimited) |
| `ephemeral` | boolean | `true` | Fresh session each run (default). Set `false` for persistent sessions (costs grow per run) |
| `grace_minutes` | integer | `15` | Staleness window — jobs missed by more than this are skipped |
| `created_at` | string | — | ISO 8601 creation timestamp |

### Report Mode

Appended as a suffix to the prompt:

- **`always`**: `[Cron job instruction: Send your findings to Discord even if there's nothing new.]`
- **`silent`**: `[Cron job instruction: Only send a message to Discord if you have something noteworthy to report. If nothing to report, do nothing.]`

### Full Prompt Format

```
[Cron job: {job_id}, report to Discord channel {channel}]

{original_prompt}

{report_mode_suffix}
```

## Schedule Expression

Standard 5-field cron format parsed by `croniter`:

```
┌───────────── minute (0-59)
│ ┌─────────── hour (0-23)
│ │ ┌───────── day of month (1-31)
│ │ │ ┌─────── month (1-12)
│ │ │ │ ┌───── day of week (0-6, 0=Sunday)
│ │ │ │ │
* * * * *
```

**Timezone**: Controlled by `CRON_TIMEZONE` in `.env` (e.g., `Europe/Paris`). Defaults to UTC.

## Session Management

- **Ephemeral jobs** (default): Fresh `UUID4()` each run — no session continuity. Cross-run context should use the memory system (KB, logs), not session history.
- **Non-ephemeral jobs**: Deterministic session via `UUID5(NAMESPACE_DNS, f"cron-job-{job_id}")` — session persists across runs, but costs grow with each run.

Session resolution uses **resume-first**: try `--resume`, fall back to `--session-id` if session doesn't exist yet.

## State & Lock Architecture

```
cron-jobs/
├── *.json                 # Job definitions (tracked in git)
├── .state/                # Per-job last-run timestamps (gitignored)
│   ├── daily-digest
│   ├── kb-gardening
│   └── ...
├── .locks/                # Per-job execution locks (gitignored)
│   ├── _history.lock
│   ├── daily-digest.lock
│   └── ...
└── .history.json          # Execution history (gitignored)
```

### Per-Job State (`.state/{job_id}`)

Plain text file containing a single ISO 8601 timestamp of the last completed run.

- `get_last_run(job_id)` → reads file, returns `datetime | None`
- `set_last_run(job_id, timestamp)` → writes file atomically
- Each job only touches its own state file — no concurrent write corruption.

### Per-Job Locks (`.locks/{job_id}.lock`)

OS-level `flock` (exclusive, non-blocking) prevents double dispatch:

- `acquire_job_lock(job_id)` → `fcntl.flock(fd, LOCK_EX | LOCK_NB)` → returns file object or `None`
- Lock held for the duration of job execution.
- Automatically released on close/crash (kernel-level).
- If lock fails, job is skipped with a warning.

### History (`.history.json`)

JSON object mapping job IDs to arrays of run entries:

```json
{
  "daily-digest": [
    {
      "timestamp": "2026-02-16T21:21:53+01:00",
      "exit_code": 0,
      "duration": 17.15,
      "session_id": "01791de5-2858-5e21-a63c-3d724ae5e394",
      "cost_usd": 0.025678
    }
  ]
}
```

- Rolling limit: 100 entries per job (oldest dropped).
- Writes protected by `.locks/_history.lock` (blocking flock).

## Staleness Guard

Prevents restart floods where all overdue jobs fire immediately after a bot restart.

**Logic in `is_job_due()`**:

1. Read `last_run` from state. If `None` (first time): initialize to now, return `False`.
2. Compute `next_run` via `croniter` from `last_run`.
3. If `now < next_run`: not due yet.
4. If `now - next_run > grace_minutes * 60`: **stale** — skip job, advance state to now, log warning.
5. Otherwise: job is due and within grace window.

**Default grace period**: 15 minutes. Configurable per-job via `grace_minutes`.

## Parallel Execution

Due jobs run concurrently via `ThreadPoolExecutor(max_workers=6)`:

1. Collect all due jobs.
2. Submit each to the pool.
3. Per-thread: acquire lock → execute → update state → append history → release lock.
4. Errors in one job don't affect others.
5. Dispatcher waits for all workers before exiting.

## Scheduler Loop

In `merlin_bot.py._cron_scheduler()`:

```python
async def _cron_scheduler() -> None:
    while True:
        now = datetime.now()
        seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(seconds_until_next_minute)
        asyncio.create_task(_run_cron_runner())
```

- Sleeps until the next minute boundary (:00 seconds).
- Fire-and-forget: spawns `cron_runner.py` as subprocess.
- Multiple dispatchers can overlap (per-job locks prevent double dispatch).
- Crash handling: non-zero exit → logged to `structured.jsonl` + Discord alert.

## CLI Management

```bash
uv run cron_manage.py add --schedule "0 9 * * *" --prompt "..." --channel <id>
uv run cron_manage.py list [--discord]
uv run cron_manage.py get <job-id> [--discord]
uv run cron_manage.py enable <job-id>
uv run cron_manage.py disable <job-id>
uv run cron_manage.py remove <job-id>
uv run cron_manage.py history [<job-id>] [--limit N] [--discord]
```

**Manual execution** (bypasses schedule, reuses logging/history):

```bash
uv run cron_runner.py --job <job-id>
```

## Logging

| Log | Purpose |
|-----|---------|
| `logs/cron_runner.log` | Dispatcher activity (one line per job check/execution) |
| `logs/claude/<timestamp>-cron-<job_id>-<session>.log` | Per-invocation Claude output |
| `logs/structured.jsonl` | `cron_dispatch` events for dashboard |

## Key Files

| File | Purpose |
|------|---------|
| `cron_runner.py` | Dispatcher (check due jobs, execute in parallel) |
| `cron_manage.py` | CLI for job management |
| `cron_state.py` | State/history/lock helpers |
| `merlin_bot.py` | `_cron_scheduler()` loop |
| `cron-jobs/*.json` | Job definitions |
