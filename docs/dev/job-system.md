# Job System

Reference documentation for the job system. Covers the module structure, the
REST API, the job file, dispatch, the run in its tmux window, the supervisor
and its finish rules, orphans and window retention, state and locks, the
hybrid log storage, the scheduler loop and the webhook trigger. The user
guide is [`docs/jobs.md`](../jobs.md).

## Overview

Jobs are a **core module** (`job/`) that runs independently of any extension.
A job has up to two optional **triggers**, set independently: a **schedule**
(cron expression) and a **webhook** (secret-gated HTTP fire). A job with
neither is manual-only. The scheduler starts from `main.py` at boot and fires
`job/runner.py` every minute. The runner loads all enabled jobs, checks which
are due, and runs them in parallel via `ThreadPoolExecutor` with per-job file
locks. Webhook fires arrive through the `webhooks/` front desk and launch
in-process (see Webhook Trigger below).

**There is one way to run a job.** Every run, agent and script alike, lives
in a window of one dedicated tmux session, `merlin-jobs`. An agent job runs
the agent's real interactive interface (Claude Code or Codex) with every
permission check bypassed, so the founder can watch a run, step in, answer it
or talk to it by voice. A command job runs `bash -lc` with its output teed
into the window. No headless mode, no persistent job session: every run starts
fresh, and what must last between runs goes in files.

```
main.py (_run -> job.start())
  └─ job/__init__.py (_job_scheduler)
       └─ spawns job/runner.py every minute
            ├─ retention.tick(): finalize orphans, close windows past retention
            └─ loads jobs/*.json, checks is_job_due() for each
                 └─ ThreadPoolExecutor(max_workers=6) for due jobs
                      └─ acquire_job_lock() -> runs.new_run() -> runs.launch()
                         -> supervisor.supervise() -> record_run()
```

## Module Structure

```
job/
├── __init__.py     # Scheduler loop (start(), _job_scheduler, _run_job_runner)
├── runner.py       # Dispatcher: due jobs, the run (launch, supervise, record), deadlines
├── agents.py       # The agent table: id, binary, launch argv, hook provider, the warning
├── runs.py         # A run: its private directory, run.sh, the prompt, the tmux window
├── supervisor.py   # Knowing a run finished: the finish rules, the start watchdog, deadlines
├── retention.py    # Each tick: orphaned runs finalized, windows closed past retention
├── state.py        # State/history/lock helpers (last_run, history, flock)
├── manage.py       # Job CRUD operations (load, save, delete, list, CLI)
├── routes.py       # REST API endpoints + /jobs dashboard page
├── schemas.py      # Pydantic models (JobCreate, JobUpdate)
├── logs.py         # Hybrid log storage (individual files + metadata index)
├── notify.py       # Notification delivery after a run (report_mode logic)
├── webhook.py      # Webhook trigger (single-flight launch, secret lifecycle)
└── templates/
    └── jobs.html   # Dashboard page template
```

Key dependencies:
- `board/sweep.py`: every tmux read and write (opening the window, the
  options, `capture-pane`, the client list, `kill-window`)
- `terminal/tmux.py`: the server-creation policy (`tmux_conf_args`,
  `server_spawn_prefix`, `terminal_process_env`) and `JOBS_SESSION_NAME`
- `lib/skills.py`: the state hooks a run depends on (`state_hooks_present`)
- `structured_log.py`: event logging (crashes, dispatch events)
- `paths.py`: file/directory resolution (`job_runs_dir`, `jobs_dir`, `job_logs_dir`)

## REST API

All endpoints require authentication. Prefix: `/api/jobs`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/jobs/jobs` | GET | List all jobs (enriched with last_run, next_run) |
| `/api/jobs/jobs` | POST | Create a new job |
| `/api/jobs/jobs/{job_id}` | GET | Get a job with history (each entry with `window` and `window_url` while its window exists) |
| `/api/jobs/jobs/{job_id}` | PUT | Update a job (merge non-None fields) |
| `/api/jobs/jobs/{job_id}` | DELETE | Delete a job + state + locks + logs |
| `/api/jobs/jobs/{job_id}/toggle` | POST | Toggle enabled/disabled |
| `/api/jobs/jobs/{job_id}/run` | POST | Trigger immediate run (202 Accepted) |
| `/api/jobs/jobs/{job_id}/logs` | GET | List execution logs (newest first, with the window link while it exists) |
| `/api/jobs/jobs/{job_id}/logs/{timestamp}` | GET | Read a specific execution log |
| `/api/jobs/logs` | GET | Execution logs across jobs, newest first |
| `/api/jobs/jobs/{job_id}/webhook` | POST | Enable the webhook trigger (generates a secret; idempotent) |
| `/api/jobs/jobs/{job_id}/webhook/rotate` | POST | Replace the secret (the old one stops working immediately) |
| `/api/jobs/jobs/{job_id}/webhook` | DELETE | Remove the webhook trigger |
| `/api/jobs/webhook-events` | GET | Recent webhook_request events (fires + rejected attempts), filterable by `job_id` |
| `/api/jobs/crashes` | GET | Recent `job_runner_crash` events (24h) |
| `/api/jobs/validate-schedule` | POST | Validate cron expression; returns `{valid, human, timezone, next_runs[3]}` preformatted in the cron timezone |

**Dashboard page**: `GET /jobs` renders the jobs management UI, with the
agents, their launch problems (binary missing, hooks not installed) and the
bypass warning for the form.

## Job File Format

**Location**: `jobs/{job-id}.json` (under the data directory resolved by `paths.py`)

Job ID is the filename without `.json` (e.g., `daily-digest.json` -> job ID `daily-digest`).

```json
{
  "description": "Human-readable summary",
  "schedule": "30 2 * * *",
  "timezone": "Europe/Paris",
  "type": "prompt",
  "agent": "claude",
  "prompt": "The prompt the agent runs",
  "enabled": true,
  "report_mode": "always",
  "grace_minutes": 15,
  "created_at": "2026-02-05T11:50:28.679794+00:00"
}
```

### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `schedule` | string | `null` | Optional 5-field cron expression (via `croniter`). Absent = no schedule trigger (webhook- or manual-only job) |
| `timezone` | string | `null` | IANA zone the schedule is interpreted in (e.g. `Europe/Paris`). `null` = fall back to server-wide `JOB_TIMEZONE`, then UTC. DST-aware (see below) |
| `type` | string | `"prompt"` | Job action type: `"prompt"` (agent) or `"command"` (shell). Absent = `"prompt"` (backward compatible) |
| `agent` | string | `"claude"` | Prompt jobs: the agent that runs the prompt, one of the ids in `job/agents.py` (`claude`, `codex`). An unknown id runs the default |
| `prompt` | string | required for `prompt` jobs | The prompt the agent runs (no delivery instructions) |
| `command` | string | required for `command` jobs | Shell command run via `bash -lc` (see Command Jobs below) |
| `working_dir` | string | `null` | Both job types: directory the job runs in. Falls back to `MERLIN_LAUNCH_CWD` then `$HOME`. Agent jobs pick up that directory's own CLAUDE.md |
| `description` | string | `""` | Human-readable summary |
| `enabled` | boolean | `true` | Toggle without deleting |
| `report_mode` | string | `"always"` | `"always"` (always notify), `"silent"` (only notify on errors), or `"off"` (never notify). Stamped on the run's window as `@job_report` |
| `grace_minutes` | integer | `15` | Staleness window: jobs missed by more than this are skipped. Internal/API-only: not exposed in the dashboard form |
| `webhook` | object | `null` | Optional webhook trigger: `{"secret": "whk_..."}`. Present = the job is webhook-firable; the secret is required and generated server-side |
| `created_at` | string | -- | ISO 8601 creation timestamp |

Fields of earlier versions (`max_turns`, `ephemeral`) still load and drop at
the next save (`manage.LEGACY_FIELDS`). `discord_channel` is the Discord
destination override, read by `notify.py`.

### The agent table (`job/agents.py`)

One small table, the seam for a future agent:

| id | launch line | hook provider |
|---|---|---|
| `claude` | `claude --dangerously-skip-permissions "$prompt"` | `claude` (`~/.claude/settings.json`) |
| `codex` | `codex --dangerously-bypass-approvals-and-sandbox "$prompt"` | `codex` (`$CODEX_HOME/hooks.json`) |

A job runs unattended by definition, so every permission check is bypassed
and there is no consent gate: a plain warning (`agents.BYPASS_WARNING`) where
a job is created (the form's help text, the stderr of `merlin job add`, so
stdout stays one JSON document) and in the docs. `launch_problem(agent)`
is what stops a run from starting: the binary missing from `PATH` (exit 127)
or Merlin's state hooks not installed in that agent's user config (exit 1,
naming `merlin config agent-state-hooks auto`). The hook check
(`lib.skills.state_hooks_present`) reads the config whatever the consent mode
says and never writes it. The form shows the same problems up front.

## A run in its window

### Starting a run (`job/runs.py`)

A run has an id (the request id: a webhook fire's `run_id`, else a fresh
UUID) and a private directory, `~/.merlin/job-runs/<run_id>/` (mode 0700,
`paths.job_runs_dir`):

| File | Written by | Holds |
|---|---|---|
| `meta.json` | `new_run`, `launch` | The run's facts: job id, the job, trigger, type, agent, cwd, start, deadline, `report_mode`, the window target once open. The orphan finalizer reads it |
| `run.sh` | `new_run` | What the window runs (below) |
| `prompt` | `new_run` | The agent's prompt: `[Job: id]` or `[Triggered by webhook: id]`, a blank line, the job's prompt. Never on a tmux command line |
| `pid` | `run.sh` | The job's process group |
| `status` | `run.sh` | The job's own exit code |
| `output.log` | `run.sh` | A command's output (`tee`) |
| `exit` | `run.sh` or the supervisor | The run's exit code, written atomically (temp file then rename), first writer wins |
| `result.json` | `record_run` | Present once the run is recorded (history and run log): the exactly-once marker |

`runs.launch` opens the window detached in session `merlin-jobs`
(`board.sweep.open_window`): when the session does not exist, it is created
with this window as its only one, through the `terminal/tmux.py` policy (the
config flag, the spawn prefix when this call creates the server, and
`terminal_process_env` as the server's environment, since tmux reads its
config and `MERLIN_TERMINAL_HOOKS` only at server creation). No stray shell
window. The window is named after the job id (`-n`, which turns automatic
rename off), opens in the job's working directory (`resolve_working_dir`:
`working_dir` then `MERLIN_LAUNCH_CWD` then `$HOME`) and runs `bash
<run.sh>`. Right after, one chained tmux call stamps its options:

- `@job_id`, `@job_run`, `@job_report` (the job's `report_mode`),
  `@job_started` (epoch, stamped last);
- `@agent_sid` = `job-<run_id>` and `@agent_cwd`, so the board, the pills and
  the attention watcher treat the window as any other agent window from its
  first second (the session init hook keeps a sid that is already set);
- `@agent_state` = `busy` for a command, `idle` for an agent until its hooks
  take over.

`run.sh` gates on `@job_started` before anything else (30 s, then it gives
up with exit 1), so the job's first hook always finds the sid set: no race
between the stamp and the agent's SessionStart. Then it unsets `CLAUDECODE`,
exports `MERLIN_JOB_ID`, `MERLIN_JOB_RUN_ID`, `MERLIN_JOB_TRIGGER`,
`MERLIN_HOME` and the forwarded `MERLIN_*` variables (`runs.FORWARDED_ENV`,
never a secret), so a job reaches the same Merlin whichever tmux server
environment its window was born under, enters the working directory and runs
the job **in its own process group** (`set -m`, the job backgrounded then
brought to the foreground, its pgid in `pid`), so a deadline can end the job
and leave the script to record the end:

- **Command**: `bash -lc <command> </dev/null 2>&1 | tee output.log`, the
  exit code taken from the command (`PIPESTATUS[0]`), not the pipe. Stdout
  is not a TTY, which is what scripts had before; stdin is `/dev/null`, so a
  script never hangs on input it never had.
- **Agent**: `exec <launch line> "$(cat prompt)"`.

When the job ends, `run.sh` writes `exit` (unless the supervisor already
did), stamps `@job_exit` (unless already set) and `@agent_state done` if the
state is not already `done`, and execs an interactive login shell so the
window stays readable.

### Knowing it finished (`job/supervisor.py`)

The supervisor is the thread holding the flock (the runner's worker, or the
webhook path's thread, which is off the event loop). It polls every
`POLL_INTERVAL` (2 s). A run is finished when any of these holds, first match
wins:

1. The `exit` file exists: exit code from the file.
2. Agent only: `@agent_state` is `done`, or it is `idle` after the supervisor
   saw it `busy` (`done` can be missed: a run watched live goes busy to idle
   when the window is selected, or on Esc): exit 0. The agent keeps running.
3. The deadline passed (`COMMAND_TIMEOUT_SECONDS` 3600,
   `AGENT_TIMEOUT_SECONDS` 21600, constants in `runner.py`): the supervisor
   writes `exit` = 124 and `@job_exit` first, then SIGTERM then SIGKILL to the
   job's process group (`end_run`), so `run.sh` stamps the end and the window
   stays. Exit 124.
4. The window vanished: exit 130, "window closed before the run ended".

An agent still not `busy` `START_WATCHDOG` (120 s) after the launch is stuck
on something no hook reports (a trust or bypass dialog on a fresh machine):
the supervisor stamps `@agent_state ask`, so the ordinary "Needs an answer"
notification fires with the pane snippet, and keeps waiting. The human can
answer in the window; the deadline still bounds the run.

At the finish (`supervisor.finish`) the window gets `@job_exit` (unless
`run.sh` wrote it) and `@job_finished` (epoch), and the record's output is
read: `output.log` for a command, the plain text of `capture-pane -p -S -`
for an agent (the whole pane, scrollback included).

### The record (`runner.record_run`)

History entries and run logs keep their shape, minus `cost_usd` and
`session_id`, plus `agent` and `run_id`. Old entries that still carry the
former read fine. `trigger` provenance, the schedule cursor rule,
`grace_minutes` and the staleness guard are unchanged. The 100 KB output cap,
the 50 logs per job and the 100 history entries stay. `record_run` marks the
run directory recorded (`result.json`): a second call for the same run (the
orphan path racing a supervisor) writes nothing. `merlin job trigger` still
blocks until the run finishes and prints one JSON document, with `run_id` and
`window`.

A launch that cannot happen (no tmux, the agent binary missing, its hooks not
installed, the window not opening) is recorded at once as a failed run with
the message as its output, and no window.

### Orphans and windows (`job/retention.py`)

A supervisor can die with its run: a Merlin restart kills a webhook thread, a
reboot kills everything. Every scheduler tick runs `retention.tick()` first:

**Orphans.** A run directory with no `result.json` whose job flock is free is
finalized from what is left (`orphan_exit`): its `exit` file; its window gone,
exit 130; an agent window `done`, or `idle` older than the start watchdog
(nobody saw it busy, so nobody can tell the difference), exit 0; its deadline
passed, exit 124 with the process group ended. A run still `busy`, `ask` or
young is left for a later tick. The flock is taken for the finalization and
`result.json` re-checked under it, so two overlapping ticks cannot both
finalize one run and nothing is recorded twice. A run still supervised holds
its flock and is never touched. A directory without `meta.json` older than
an hour was abandoned mid-creation and is removed.

**Windows do not pile up.** Each tick closes `merlin-jobs` windows and
removes their run directories (`should_close` is the pure rule):

- a window a tmux client is showing right now (`sweep.displayed_windows`,
  from `list-clients`) is never closed;
- finished with exit 0 and no pane activity since (`window_activity` within
  `ACTIVITY_SLACK`, 5 s, of `@job_finished`: the finish's own output): closed
  after 1 hour, or at the next run of the same job (a newer `@job_started` or
  run directory of that job), whichever comes first;
- failed, or activity after the finish (someone stepped in): closed 24 hours
  after the last activity;
- closing never touches a window without `@job_run`, nor any other session.

A recorded run whose window is already gone loses its directory at once. A
tick with no `merlin-jobs` session and no run directory does no work.

`workspace/` neither snapshots nor restores `merlin-jobs`
(`terminal.tmux.JOBS_SESSION_NAME`): a job run is not part of the workspace.

### Command Jobs

A job with `"type": "command"` runs a shell command instead of an agent. It
executes with the same privileges as the web Terminal, in a `merlin-jobs`
window like every run. Its output is the `output.log` the window's `tee`
wrote. `report_mode="silent"` notifies only on a non-zero exit, so it is the
natural default for backups and maintenance commands.

### Report Mode

Handled after a run, not in the prompt:

- **`always`** (default): notify after every run.
- **`silent`**: only on errors (non-zero exit code).
- **`off`**: never.

The dashboard modal exposes this as a single **Notify** select (Always / Errors
only / Never).

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

**Timezone**: Each job carries an optional per-job `timezone` (IANA name). The runner resolves a job's zone in order: per-job `timezone` -> server-wide `JOB_TIMEZONE` (`.env`; the old `CRON_TIMEZONE` name is still honored) -> UTC (`runner.py:job_timezone()`). `is_job_due()` interprets the schedule in that zone, so a wall-clock schedule like `0 17 * * *` fires at 17:00 **local** and stays 17:00 across DST transitions (storing local-time + zone, not a frozen UTC offset). The shared helper `job/tz.py:job_timezone_default()` resolves the server-wide default (UTC fallback) and is used by the preview/enrich helpers in `routes.py`, which prefer the job's own zone, so the dashboard's next-run preview and the cards' "Next" reflect when the job actually fires. The modal's timezone selector defaults to the browser's zone for new jobs.

> **DST edge**: on a "spring-forward" day a wall-clock time in the skipped hour (e.g. 02:30 where the clock jumps 02:00 to 03:00) resolves to the next valid instant; this is `croniter`/`zoneinfo` behavior and is acceptable.

**Human-readable descriptions**: `manage.py:cron_to_human()` delegates to the `cron-descriptor` library so every valid expression (including raw Custom ones) gets a correct English description; it falls back to the raw expression on error.

## State & Lock Architecture

```
jobs/
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
- Each job only touches its own state file: no concurrent write corruption.

### Per-Job Locks (`.locks/{job_id}.lock`)

OS-level `flock` (exclusive, non-blocking) prevents double dispatch:

- `acquire_job_lock(job_id)` -> `fcntl.flock(fd, LOCK_EX | LOCK_NB)` -> returns file object or `None`
- Held for the duration of the run, deadline included: a deadline always
  releases it.
- Automatically released on close/crash (kernel-level), which is what lets
  the orphan path take over a run whose supervisor died.
- If lock fails, job is skipped with a warning.

### History (`.history.json`)

JSON object mapping job IDs to arrays of run entries (metadata only, no output):

```json
{
  "daily-digest": [
    {
      "timestamp": "2026-02-16T21:21:53+01:00",
      "exit_code": 0,
      "duration": 17.15,
      "trigger": "schedule",
      "agent": "claude",
      "run_id": "0d2c7a1e-..."
    }
  ]
}
```

- Rolling limit: 100 entries per job (oldest dropped).
- Writes protected by `.locks/_history.lock` (blocking flock).

## Hybrid Log Storage

Individual execution logs are stored separately from the metadata index to keep `.history.json` small while preserving full output.

**Location**: `~/.merlin/job-logs/{job-id}/{timestamp}.json`

Each log file contains:

```json
{
  "job_id": "daily-digest",
  "timestamp": "2026-03-22T02:30:00+00:00",
  "exit_code": 0,
  "duration_seconds": 17.15,
  "trigger": "schedule",
  "agent": "claude",
  "run_id": "0d2c7a1e-...",
  "output": "The agent's pane, or the command's output",
  "output_truncated": false
}
```

- Output is capped at 100KB; `output_truncated` is set if trimmed.
- Default max 50 log files per job (oldest pruned automatically).
- Timestamp format in filenames is ISO-like but filesystem-safe (colons replaced with hyphens).
- The API adds `window` and `window_url` (the terminal deep link, the same a
  notification carries) to an entry whose run window still exists
  (`runs.job_windows`): a run is read through its window while it lasts, and
  always through its run log.

## Staleness Guard

**The user-facing contract is plain cron**: jobs fire on their schedule; a run
missed during a brief restart still fires up to 15 minutes late; longer gaps
are skipped (never a catch-up flood after downtime). The grace window is an
internal detail: it is not shown in the dashboard form. Per-job
`grace_minutes` in the job file (or via the API) overrides the default for
power users.

**Logic in `is_job_due()`**:

1. Read `last_run` from state. If `None` (first time): initialize to now, return `False`.
2. Compute `next_run` via `croniter` from `last_run`.
3. If `now < next_run`: not due yet.
4. If `now - next_run > max(grace_minutes * 60, 59)`: **stale**: skip job, advance state to now, log warning. (Sub-minute staleness is never "missed": the dispatcher always fires a second or two after the minute, so `grace_minutes: 0` would otherwise mean "never run".)
5. Otherwise: job is due and within grace window.

**Default grace period**: 15 minutes. Multiple missed slots within the window
coalesce into a single catch-up run, never a burst.

**Restart during a run**: the runner is a separate process and survives a
Merlin restart, so a scheduled run keeps its supervisor. A webhook run's
supervisor is a thread of the Merlin process and dies with it: the run goes
on in its window and the orphan path records it at a later tick. State is
marked *before* execution, so the slot is never double-fired; the flock blocks
re-dispatch while the run is still executing.

## Parallel Execution

Due jobs run concurrently via `ThreadPoolExecutor(max_workers=6)`:

1. Collect all due jobs.
2. Submit each to the pool.
3. Per-thread: acquire lock -> open the window -> supervise -> record -> release lock.
4. Errors in one job don't affect others.
5. Dispatcher waits for all workers before exiting, so a runner process lives
   as long as its longest run (up to the deadline).

## Scheduler Loop

In `job/__init__.py._job_scheduler()`:

```python
async def _job_scheduler() -> None:
    while True:
        now = datetime.now()
        seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(seconds_until_next_minute)
        asyncio.create_task(_run_job_runner())
```

- Started from `main.py` via `job.start()` as a core feature (always runs).
- Sleeps until the next minute boundary (:00 seconds).
- Fire-and-forget: spawns `job/runner.py` as subprocess.
- Multiple dispatchers can overlap (per-job locks prevent double dispatch,
  and the orphan path takes the flock too).
- Crash handling: non-zero exit -> logged to `engine-log.jsonl`.

## Webhook Trigger

An external HTTP call can launch a job exactly the way the scheduler or a
manual run does. The flow is split between two modules:

- **`webhooks/` (the front desk)** owns the shared, security-sensitive
  plumbing: the single public route `POST /webhooks/{source}/{target_id}`
  (mounted in `main.py` WITHOUT `require_auth`, the terminal precedent),
  constant-time secret verification (`X-Merlin-Webhook-Secret` header or
  `?token=` fallback) checked BEFORE anything else that could reject the
  request so a valid secret is never blocked, target-id regex validation,
  and logging of every request outcome as a `webhook_request` event. There
  is deliberately **no in-app rate limiter**: the 256-bit secret makes
  throttling failed guesses pointless, and bounding a junk flood is an edge
  concern (our Caddy on SaaS, the operator's own reverse proxy when
  self-hosting) consistent with Merlin's bring-your-own-exposure model.
- **`job/webhook.py` (the `job` source)** owns everything job-specific:
  resolving a job id to a target (a job without a `webhook` block is a 404,
  same as a missing job), the secret lifecycle (`whk_` +
  `secrets.token_urlsafe`), single-flight launching, and the public URL.

**Single-flight per job**: one active run at a time. A fire while a run is
in flight is coalesced: it returns 200/`"coalesced"` with the active run's
id and starts nothing (BetterStack firing five times during one incident
spawns exactly one run). An in-process in-flight map covers
webhook-vs-webhook overlap; the per-job flock covers overlap with
scheduled/manual runs. Launched runs execute in a worker thread
(`asyncio.to_thread` around `runner._execute_job`, so the event loop is
never blocked by the supervisor's polling); the flock is released as soon as
the run finishes, before the (also off-loop) notification, so single-flight
covers the run, not the delivery. The accepted `run_id` doubles as the run's
id and `request_id`, so `webhook_request` and `job_dispatch` events
correlate and the run directory is `job-runs/<run_id>/`.

**Response codes**: `200 {ok, status, run_id}` (launched or coalesced; the
run itself is asynchronous), `401` bad/missing secret, `403` disabled
job, `404` unknown source/job/no-webhook.

**Trigger provenance**: every run records `trigger` (schedule / webhook /
manual) in `job_dispatch` events, history entries, and run logs: unified
but filterable. A webhook or manual run does **not** advance the schedule
cursor (`last_run`), so a fire that overlaps a scheduled slot can't swallow
it: the slot stays due and the grace window fires it.

**Public URL** (`merlin job url`, shown in the editor): resolution order is
`MERLIN_DASHBOARD_URL` (explicit operator override, own tunnel/proxy, the
one undiscoverable case) -> the portal's `GET /api/instance/whoami` answer
(any SaaS instance, BYOI and managed alike; token-authenticated, resolved at
read time with a 5-minute in-process memo so an environment rename is picked
up automatically and never cached stale on disk) -> `MERLIN_ENVIRONMENT_SLUG`
(managed-container fallback when the portal is unreachable) -> detected local
IP + bound port (may be NAT-private; reachability is the operator's job). The
fire path itself needs none of this: it rides the SaaS proxy with zero
portal involvement; whoami only makes the *displayed* URL correct.

Registration: `main.py` calls `webhooks.register("job", job_webhook.resolve)`
directly. Extensions can export `WEBHOOK_HANDLERS = {source: resolver}`; the
extension loader wires them into the same front desk (an extension cannot
open its own unauthenticated route: the loader force-wraps extension
routers in auth).

## CLI Management

```bash
merlin job add --schedule "0 9 * * *" --prompt "..." --description "..."
merlin job add --prompt "..." --agent codex --webhook   # webhook-only Codex job
merlin job list
merlin job get <job-id>
merlin job enable <job-id>
merlin job disable <job-id>
merlin job remove <job-id>
merlin job trigger <job-id>                     # blocks until the run finishes
merlin job history [<job-id>] [--limit N]
merlin job webhook <job-id> [--enable|--disable|--rotate]
merlin job url <job-id>                          # public URL + secret + curl
merlin job test <job-id> [--port N]              # fire the hook locally
```

`merlin job add` prints the bypass warning on stderr; stdout stays one JSON
document.

**Manual execution** (bypasses schedule, reuses logging/history):

```bash
uv run job/runner.py --job <job-id>
```

## Tests

- `tests/unit/test_job_windows.py`: against a real tmux server isolated by
  `TMUX_TMPDIR`, with a fake agent on `PATH` that drives the state hooks like
  the real ones and temporary agent configs: an agent run from launch to
  `done`, a Codex launch line, a run watched live (busy to idle), a crash, a
  stuck start, a deadline ending the process group, a refused launch (hooks,
  binary), a failing command, closed stdin, the job environment under a
  foreign server, a window closed mid-run, a prompt with shell
  metacharacters, orphans under overlapping ticks, a coalesced webhook, the
  retention rules (pure and real), a displayed window, the workspace
  exclusion.
- `tests/unit/test_job_core_runner.py` and friends: the dispatcher, the
  record and the CLI with the launch and the supervisor replaced by the
  `fake_launch` fixture (`tests/unit/conftest.py`).

No test ever touches the default tmux server (`tests/conftest.py` points
`TMUX_TMPDIR` at a private directory for the whole session), the real
`~/.claude/settings.json` or `~/.codex/`.

## Logging

| Log | Purpose |
|-----|---------|
| `~/.merlin/job-logs/{job-id}/*.json` | Per-execution full output (hybrid storage) |
| `~/.merlin/job-runs/{run_id}/` | A run's script, prompt, exit code and output while its window lives |
| `logs/engine-log.jsonl` | `job_dispatch` and `job_runner_crash` events |

## Key Files

| File | Purpose |
|------|---------|
| `job/__init__.py` | Scheduler loop (`_job_scheduler`, `start()`) |
| `job/runner.py` | Dispatcher (due jobs, the run, the record, the deadlines) |
| `job/agents.py` | The agent table and the launch checks |
| `job/runs.py` | The run directory, `run.sh`, the window and its options |
| `job/supervisor.py` | The finish rules, the start watchdog, ending a process group |
| `job/retention.py` | Orphan finalization and window retention, every tick |
| `job/manage.py` | Job CRUD + implementation of `merlin job` |
| `job/state.py` | State/history/lock helpers |
| `job/schemas.py` | Pydantic models for REST API validation |
| `job/routes.py` | REST API endpoints + dashboard page |
| `job/logs.py` | Hybrid log storage (individual files + metadata) |
| `job/notify.py` | Notification delivery after a run (report_mode) |
| `job/webhook.py` | Webhook trigger: resolver, single-flight launch, secret lifecycle, public URL |
| `webhooks/__init__.py` | The front desk: public route, secret check, request logging |
| `job/templates/jobs.html` | Dashboard page template |
| `board/sweep.py` | Every tmux read and write a run needs |
| `terminal/tmux.py` | The server-creation policy and `JOBS_SESSION_NAME` |
| `jobs/*.json` | Job definitions |
