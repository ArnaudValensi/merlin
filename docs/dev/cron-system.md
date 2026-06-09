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
| `/api/cron/validate-schedule` | POST | Validate cron expression; returns `{valid, human, timezone, next_runs[3]}` preformatted in the cron timezone |

**Dashboard page**: `GET /cron` — renders the cron management UI.

## Job File Format

**Location**: `cron-jobs/{job-id}.json` (under the data directory resolved by `paths.py`)

Job ID is the filename without `.json` (e.g., `daily-digest.json` -> job ID `daily-digest`).

```json
{
  "description": "Human-readable summary",
  "schedule": "30 2 * * *",
  "timezone": "Europe/Paris",
  "type": "prompt",
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
| `timezone` | string | `null` | IANA zone the schedule is interpreted in (e.g. `Europe/Paris`). `null` = fall back to server-wide `CRON_TIMEZONE`, then UTC. DST-aware (see below) |
| `type` | string | `"prompt"` | Job action type: `"prompt"` (agent) or `"command"` (shell). Absent = `"prompt"` (backward compatible) |
| `prompt` | string | required for `prompt` jobs | Prompt sent to the engine (no delivery instructions — engine is a black box) |
| `command` | string | required for `command` jobs | Shell command run via `bash -lc` (see Command Jobs below) |
| `working_dir` | string | `null` | Both job types: directory the job runs in. Falls back to `MERLIN_LAUNCH_CWD` then `$HOME`. Agent jobs pick up that directory's own CLAUDE.md |
| `discord_channel` | string | `null` | Discord channel ID for notifications (optional — falls back to bot default) |
| `description` | string | `""` | Human-readable summary |
| `enabled` | boolean | `true` | Toggle without deleting |
| `report_mode` | string | `"always"` | `"always"` (always notify), `"silent"` (only notify on errors), or `"off"` (never notify) — handled by `notify.py`, not the prompt |
| `max_turns` | integer | `0` | Prompt jobs only: max agentic turns (0 = unlimited) |
| `ephemeral` | boolean | `true` | Prompt jobs only: fresh session each run (default). Set `false` for persistent sessions (costs grow per run) |
| `grace_minutes` | integer | `15` | Staleness window — jobs missed by more than this are skipped. Internal/API-only: not exposed in the dashboard form |
| `created_at` | string | -- | ISO 8601 creation timestamp |

> **Note**: The `discord_channel` field is optional. If omitted, notifications fall back to the bot's default channel (from `DISCORD_CHANNEL_IDS`). If the bot extension is not loaded, notifications are silently skipped. The legacy `channel` field has been removed and is no longer read anywhere.

### Command Jobs

A job with `"type": "command"` runs an arbitrary shell command instead of invoking
the agent. It executes with the same privileges as the web Terminal — no new attack
surface, no agent, no token cost (`cost_usd` is always `null`).

- **Execution**: `_run_command()` in `runner.py` runs `bash -lc <command>` via
  `subprocess.run(..., capture_output=True, text=True)`, combining stdout + stderr
  into the run output. Timed with a monotonic clock.
- **Working directory**: resolved in order `job.working_dir` → `MERLIN_LAUNCH_CWD`
  (the directory where `main.py` was launched, captured at startup and inherited by
  the runner subprocess) → `Path.home()`.
- **Timeout**: `COMMAND_TIMEOUT_SECONDS = 3600` (module constant in `runner.py`). A
  command exceeding it is killed and recorded with `exit_code = 124`, so a hung
  command can't hold its per-job flock forever.
- **Shared machinery**: `_execute_job()` branches on `type` near the top — `command`
  → `_run_command()`, otherwise `_run_agent()`. Both return the same result shape
  (`exit_code`, `duration`, `result`/output, `cost_usd`, `session_id`, `stderr`), so
  state, history, hybrid logs, `cron_dispatch` events, and `notify.py` are all shared.
  Because `report_mode="silent"` notifies only on non-zero exit, it is the natural
  default for backups/maintenance commands.
- **Performance tab**: command runs do **not** emit `invocation` engine-log events
  (only the agent `invoke()` path does), so they do not appear on the cron Performance
  tab, which aggregates invocations. They do appear in the Jobs and Logs tabs.

### Report Mode

Handled by `notify.py` after job execution (not in the prompt):

- **`always`** (default): Always send the engine's output to Discord.
- **`silent`**: Only notify on errors (non-zero exit code). Successful silent jobs produce no Discord notification.
- **`off`**: Never notify, regardless of outcome.

The dashboard modal exposes this as a single **Notify** select (Always / Errors
only / Never) plus an optional channel override. `report_mode` decides *when*
to notify; `discord_channel` is only the *destination* (it falls back to the
bot's default channel when unset or `"default"`).

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

**Timezone**: Each job carries an optional per-job `timezone` (IANA name). The runner resolves a job's zone in order: per-job `timezone` → server-wide `CRON_TIMEZONE` (`.env`) → UTC (`runner.py:job_timezone()`). `is_job_due()` interprets the schedule in that zone, so a wall-clock schedule like `0 17 * * *` fires at 17:00 **local** and stays 17:00 across DST transitions (storing local-time + zone, not a frozen UTC offset). The shared helper `cron/tz.py:cron_timezone()` resolves the server-wide default (UTC fallback) and is used by the preview/enrich helpers in `routes.py`, which prefer the job's own zone — so the dashboard's next-run preview and the cards' "Next" reflect when the job actually fires. The modal's timezone selector defaults to the browser's zone for new jobs.

> **DST edge**: on a "spring-forward" day a wall-clock time in the skipped hour (e.g. 02:30 where the clock jumps 02:00→03:00) resolves to the next valid instant; this is `croniter`/`zoneinfo` behavior and is acceptable.

**Human-readable descriptions**: `manage.py:cron_to_human()` delegates to the `cron-descriptor` library so every valid expression (including raw Custom ones) gets a correct English description; it falls back to the raw expression on error.

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

1. Check `report_mode`: if `off`, skip; if `silent` and exit_code == 0, skip.
2. If merlin-bot extension is loaded, send a formatted Discord message.
3. Channel resolution: per-job `discord_channel` → bot's default `DISCORD_CHANNEL_IDS` → skip silently.
4. Never raises — all errors are caught and logged.

The engine has no notion of Discord or delivery. It returns text output, and `notify.py` decides whether and where to deliver it. This design means cron works standalone without Discord. When the bot extension is enabled, you get Discord notifications for free.

## Staleness Guard

**The user-facing contract is plain cron**: jobs fire on their schedule; a run
missed during a brief restart still fires up to 15 minutes late; longer gaps
are skipped (never a catch-up flood after downtime). The grace window is an
internal detail — it is not shown in the dashboard form. Per-job
`grace_minutes` in the job file (or via the API) overrides the default for
power users.

**Logic in `is_job_due()`**:

1. Read `last_run` from state. If `None` (first time): initialize to now, return `False`.
2. Compute `next_run` via `croniter` from `last_run`.
3. If `now < next_run`: not due yet.
4. If `now - next_run > max(grace_minutes * 60, 59)`: **stale** — skip job, advance state to now, log warning. (Sub-minute staleness is never "missed": the dispatcher always fires a second or two after the minute, so `grace_minutes: 0` would otherwise mean "never run".)
5. Otherwise: job is due and within grace window.

**Default grace period**: 15 minutes. Multiple missed slots within the window
coalesce into a single catch-up run, never a burst.

**Restart during a run**: the runner is a separate process and survives a
Merlin restart — the in-flight job completes and writes history/logs; only its
Discord notification is lost (the stdout pipe to the dead parent). State is
marked *before* execution, so the slot is never double-fired; the flock blocks
re-dispatch while the orphaned run is still executing.

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
merlin cron add --schedule "0 9 * * *" --prompt "..." --description "..."
merlin cron list
merlin cron get <job-id>
merlin cron enable <job-id>
merlin cron disable <job-id>
merlin cron remove <job-id>
merlin cron trigger <job-id>
merlin cron history [<job-id>] [--limit N]
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
| `cron/manage.py` | Job CRUD + implementation of `merlin cron` |
| `cron/state.py` | State/history/lock helpers |
| `cron/schemas.py` | Pydantic models for REST API validation |
| `cron/routes.py` | REST API endpoints + dashboard page |
| `cron/logs.py` | Hybrid log storage (individual files + metadata) |
| `cron/notify.py` | Notification delivery (report_mode, Discord fallback) |
| `cron/templates/cron.html` | Dashboard page template |
| `lib/engine.py` | AgentEngine abstraction (used by runner) |
| `lib/session.py` | JSONL session manager |
| `cron-jobs/*.json` | Job definitions |
