# Cron System

Reference documentation for the scheduled job system. Covers module structure, REST API, job files, dispatch, state tracking, locking, hybrid log storage, notifications, and the scheduler loop.

## Overview

Cron is a **core module** (`cron/`) that runs independently of any extension. The scheduler starts from `main.py` at boot and fires `cron/runner.py` every minute. The runner loads all enabled jobs, checks which are due, and executes them in parallel via `ThreadPoolExecutor` with per-job file locks.

```
main.py (_run → cron.start())
  └─ cron/__init__.py (_cron_scheduler)
       └─ spawns cron/runner.py every minute
            └─ loads cron-jobs/*.json
            └─ checks is_job_due() for each
            └─ ThreadPoolExecutor(max_workers=6) for due jobs
                 └─ acquire_job_lock() → invoke() → set_last_run() → append_history()
```

## Module Structure

```
cron/
├── __init__.py     # Scheduler loop (start(), _cron_scheduler, _run_cron_runner)
├── runner.py       # Job dispatcher (check due, execute in parallel via lib.engine)
├── state.py        # State/history/lock helpers (last_run, history, flock)
├── manage.py       # Job CRUD operations (load, save, delete, list, CLI)
├── routes.py       # REST API endpoints + /cron dashboard page
├── schemas.py      # Pydantic models (JobCreate, JobUpdate)
├── logs.py         # Hybrid log storage (individual files + metadata index)
├── notify.py       # Notification delivery (report_mode logic, Discord fallback)
└── templates/
    └── cron.html   # Dashboard page template
```

Key dependencies:
- `lib/engine.py` — AgentEngine abstraction (used by runner.py)
- `structured_log.py` — event logging (crashes, dispatch events)
- `paths.py` — file/directory resolution

## REST API

All endpoints require authentication. Prefix: `/api/cron`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cron/jobs` | GET | List all jobs (enriched with last_run, next_run) |
| `/api/cron/jobs` | POST | Create a new job |
| `/api/cron/jobs/{job_id}` | GET | Get a job with history |
| `/api/cron/jobs/{job_id}` | PUT | Update a job (merge non-None fields) |
| `/api/cron/jobs/{job_id}` | DELETE | Delete a job + state + locks + logs |
| `/api/cron/jobs/{job_id}/toggle` | POST | Toggle enabled/disabled |
| `/api/cron/jobs/{job_id}/run` | POST | Trigger immediate run (202 Accepted) |
| `/api/cron/jobs/{job_id}/logs` | GET | List execution logs (newest first) |
| `/api/cron/jobs/{job_id}/logs/{timestamp}` | GET | Read a specific execution log |
| `/api/cron/validate-schedule` | POST | Validate cron expression, return next 3 runs |

**Dashboard page**: `GET /cron` — renders the cron management UI.

## Job File Format

**Location**: `cron-jobs/{job-id}.json` (under the data directory resolved by `paths.py`)

Job ID is the filename without `.json` (e.g., `daily-digest.json` -> job ID `daily-digest`).

```json
{
  "description": "Human-readable summary",
  "schedule": "30 2 * * *",
  "prompt": "The prompt sent to Claude when the job runs",
  "discord_channel": "YOUR_CHANNEL_ID",
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
| `prompt` | string | **required** | Prompt sent to the engine (no delivery instructions — engine is a black box) |
| `discord_channel` | string | `null` | Discord channel ID for notifications (optional — falls back to bot default) |
| `description` | string | `""` | Human-readable summary |
| `enabled` | boolean | `true` | Toggle without deleting |
| `report_mode` | string | `"always"` | `"always"` (always notify) or `"silent"` (only notify on errors) — handled by `notify.py`, not the prompt |
| `max_turns` | integer | `0` | Max agentic turns (0 = unlimited) |
| `ephemeral` | boolean | `true` | Fresh session each run (default). Set `false` for persistent sessions (costs grow per run) |
| `grace_minutes` | integer | `15` | Staleness window — jobs missed by more than this are skipped |
| `created_at` | string | -- | ISO 8601 creation timestamp |

> **Note**: The `discord_channel` field is optional. If omitted, notifications fall back to the bot's default channel (from `DISCORD_CHANNEL_IDS`). If the bot extension is not loaded, notifications are silently skipped. The legacy `channel` field (required in the old format) is no longer used.

### Report Mode

Handled by `notify.py` after job execution (not in the prompt):

- **`always`** (default): Always send the engine's output to Discord.
- **`silent`**: Only notify on errors (non-zero exit code). Successful silent jobs produce no Discord notification.

### Full Prompt Format

```
[Cron job: {job_id}]

{original_prompt}
```

The prompt contains no delivery instructions. The engine returns plain text output, and the notification system handles delivery.

## Schedule Expression

Standard 5-field cron format parsed by `croniter`:

```
+-------------- minute (0-59)
| +------------ hour (0-23)
| | +---------- day of month (1-31)
| | | +-------- month (1-12)
| | | | +------ day of week (0-6, 0=Sunday)
| | | | |
* * * * *
```

**Timezone**: Controlled by `CRON_TIMEZONE` in `.env` (e.g., `Europe/Paris`). Defaults to UTC.

## Session Management

- **Ephemeral jobs** (default): Fresh `UUID4()` each run — no session continuity. Cross-run context should use the notes system (KB, logs), not session history.
- **Non-ephemeral jobs**: Deterministic session via `UUID5(NAMESPACE_DNS, f"cron-job-{job_id}")` — session persists across runs, but costs grow with each run.

Session history is managed by Merlin's session manager (`lib/session.py`) as JSONL files at `~/.merlin/sessions/<session_id>.jsonl`. The engine receives the full conversation history on each invocation — no reliance on any engine's built-in resume mechanism.

## State & Lock Architecture

```
cron-jobs/
+-- *.json                 # Job definitions (tracked in git)
+-- .state/                # Per-job last-run timestamps (gitignored)
|   +-- daily-digest
|   +-- kb-gardening
|   +-- ...
+-- .locks/                # Per-job execution locks (gitignored)
|   +-- _history.lock
|   +-- daily-digest.lock
|   +-- ...
+-- .history.json          # Execution history metadata index (gitignored)
```

### Per-Job State (`.state/{job_id}`)

Plain text file containing a single ISO 8601 timestamp of the last completed run.

- `get_last_run(job_id)` -> reads file, returns `datetime | None`
- `set_last_run(job_id, timestamp)` -> writes file atomically
- Each job only touches its own state file — no concurrent write corruption.

### Per-Job Locks (`.locks/{job_id}.lock`)

OS-level `flock` (exclusive, non-blocking) prevents double dispatch:

- `acquire_job_lock(job_id)` -> `fcntl.flock(fd, LOCK_EX | LOCK_NB)` -> returns file object or `None`
- Lock held for the duration of job execution.
- Automatically released on close/crash (kernel-level).
- If lock fails, job is skipped with a warning.

### History (`.history.json`)

JSON object mapping job IDs to arrays of run entries (metadata only — no output):

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

## Hybrid Log Storage

Individual execution logs are stored separately from the metadata index to keep `.history.json` small while preserving full output.

**Location**: `~/.merlin/cron-logs/{job-id}/{timestamp}.json`

Each log file contains:

```json
{
  "job_id": "daily-digest",
  "timestamp": "2026-03-22T02:30:00+00:00",
  "exit_code": 0,
  "duration_seconds": 17.15,
  "cost_usd": 0.025678,
  "session_id": "01791de5-...",
  "output": "Full Claude output...",
  "output_truncated": false
}
```

- Output is capped at 100KB; `output_truncated` is set if trimmed.
- Default max 50 log files per job (oldest pruned automatically).
- Timestamp format in filenames is ISO-like but filesystem-safe (colons replaced with hyphens).

## Notification System

`cron/notify.py` provides graceful notification after job execution:

1. Check `report_mode`: if `silent` and exit_code == 0, skip notification.
2. If merlin-bot extension is loaded, send a formatted Discord message.
3. Channel resolution: per-job `discord_channel` → legacy `channel` → bot's default `DISCORD_CHANNEL_IDS` → skip silently.
4. Never raises — all errors are caught and logged.

The engine has no notion of Discord or delivery. It returns text output, and `notify.py` decides whether and where to deliver it. This design means cron works standalone without Discord. When the bot extension is enabled, you get Discord notifications for free.

## Staleness Guard

Prevents restart floods where all overdue jobs fire immediately after a restart.

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
3. Per-thread: acquire lock -> execute -> update state -> append history -> release lock.
4. Errors in one job don't affect others.
5. Dispatcher waits for all workers before exiting.

## Scheduler Loop

In `cron/__init__.py._cron_scheduler()`:

```python
async def _cron_scheduler() -> None:
    while True:
        now = datetime.now()
        seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(seconds_until_next_minute)
        asyncio.create_task(_run_cron_runner())
```

- Started from `main.py` via `cron.start()` as a core feature (always runs).
- Sleeps until the next minute boundary (:00 seconds).
- Fire-and-forget: spawns `cron/runner.py` as subprocess.
- Multiple dispatchers can overlap (per-job locks prevent double dispatch).
- Crash handling: non-zero exit -> logged to `engine-log.jsonl`.

## CLI Management

```bash
uv run cron/manage.py add --schedule "0 9 * * *" --prompt "..." --description "..."
uv run cron/manage.py list
uv run cron/manage.py get <job-id>
uv run cron/manage.py enable <job-id>
uv run cron/manage.py disable <job-id>
uv run cron/manage.py remove <job-id>
uv run cron/manage.py history [<job-id>] [--limit N]
```

**Manual execution** (bypasses schedule, reuses logging/history):

```bash
uv run cron/runner.py --job <job-id>
```

## Logging

| Log | Purpose |
|-----|---------|
| `~/.merlin/cron-logs/{job-id}/*.json` | Per-execution full output (hybrid storage) |
| `logs/engine-log.jsonl` | `cron_dispatch` and `cron_runner_crash` events |

## Key Files

| File | Purpose |
|------|---------|
| `cron/__init__.py` | Scheduler loop (`_cron_scheduler`, `start()`) |
| `cron/runner.py` | Dispatcher (check due jobs, execute in parallel) |
| `cron/manage.py` | Job CRUD + CLI for management |
| `cron/state.py` | State/history/lock helpers |
| `cron/schemas.py` | Pydantic models for REST API validation |
| `cron/routes.py` | REST API endpoints + dashboard page |
| `cron/logs.py` | Hybrid log storage (individual files + metadata) |
| `cron/notify.py` | Notification delivery (report_mode, Discord fallback) |
| `cron/templates/cron.html` | Dashboard page template |
| `lib/engine.py` | AgentEngine abstraction (used by runner) |
| `lib/session.py` | JSONL session manager |
| `cron-jobs/*.json` | Job definitions |
