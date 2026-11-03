# Merlin — System Architecture

Merlin is a suite of dev tools built around the user's own AI agent CLI
(Claude Code or Codex), run as it is: Merlin never wraps an agent, drives
it through a subprocess API, or keeps a conversation of its own. Two
pillars, one process: the **coding environment** (the web terminal where
the agent works, its windows watched and notified) and **agent
orchestration** (jobs, each run in its own tmux window). Everything else is
an extension.

## System at a glance

Four planes, one process:

- **Dashboard surface**: files, terminal, commits, the Sessions board, the
  Jobs page, Settings and Extensions, served by FastAPI (see
  [`dashboard-architecture.md`](dashboard-architecture.md),
  [`web-terminal.md`](web-terminal.md)). Built-in extensions add pages:
  Notes (off by default) and the private agent activity Timeline (behind
  `MERLIN_FEATURES=timeline`, see
  [`agent-activity-timeline.md`](agent-activity-timeline.md)).
- **Agent windows**: every agent runs in a tmux window. Hooks Merlin
  installs in the agent's own settings (with consent) stamp the window's
  state and conversation as tmux options; the attention watcher sweeps them
  and notifies (see [`notifications.md`](notifications.md),
  [`workspace-restore.md`](workspace-restore.md)).
- **Scheduling**: the job core module launching runs in the `merlin-jobs`
  tmux session and supervising them to their record (see
  [`job-system.md`](job-system.md)).
- **Extensibility**: extensions contribute commands, skills, and pages (see
  [`extension-system.md`](extension-system.md),
  [`skill-system.md`](skill-system.md)).

## Entry Points

```
╔═══════════════════════════════════════════════════════════════════════════╗
║                              ENTRY POINTS                                 ║
╠═════════════════════════╦═════════════════════════╦═══════════════════════╣
║  YOU (browser, phone)   ║  TIME (the scheduler)   ║  THE WORLD (webhook)  ║
║  open the web terminal  ║  fires every minute     ║  POST /webhooks/job/… ║
╚════════════╤════════════╩════════════╤════════════╩═══════════╤═══════════╝
             │                         │                        │
             ▼                         ▼                        ▼
┌────────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
│ terminal/              │  │ job/runner.py        │  │ webhooks/ front desk │
│ xterm.js over a PTY    │  │ (spawned subprocess) │  │ secret check, then   │
│ attached to tmux       │  │ due jobs, retention  │  │ job/webhook.py fires │
└────────────┬───────────┘  └──────────┬───────────┘  └──────────┬───────────┘
             │                         │                        │
             │ you launch claude       │ runs.launch(): a window in the
             │ or codex yourself       │ merlin-jobs session, the agent's
             │                         │ real interface, permissions bypassed
             ▼                         ▼                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                          tmux windows                                    │
│  @agent_state  idle | busy | ask | done     (stamped by the agent hooks)  │
│  @agent_conv   the conversation id          (workspace restore, voice)   │
│  @job_id @job_run @job_exit @job_finished   (a job run's window only)    │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │ board/sweep.py reads the options
             ┌─────────────────┼──────────────────────┐
             ▼                 ▼                      ▼
┌──────────────────┐ ┌──────────────────────┐ ┌───────────────────────────┐
│ Sessions board   │ │ notifications/       │ │ job/supervisor.py          │
│ (the terminal's  │ │ attention watcher:   │ │ finish rules, deadline,    │
│  drawer, pills)  │ │ page events, Web Push│ │ then runner.record_run()   │
└──────────────────┘ └──────────────────────┘ └───────────────────────────┘
```

## The Agent

Merlin has no agent engine. In the terminal you launch `claude` or `codex`
yourself; a job run does the same in a window of its own (`job/agents.py`
holds the launch lines, `claude --dangerously-skip-permissions` and
`codex --dangerously-bypass-approvals-and-sandbox`, and the warning the UI
and the CLI show). What the agent gets from Merlin:

- **Skills**: the canonical skill set (`~/.merlin/skills`) mirrored into
  `~/.claude/skills`, `~/.agents/skills` and `$CODEX_HOME/skills` at every
  start and by `merlin setup` (see [`skill-system.md`](skill-system.md)).
  `merlin agent` prints the brain doc (`agent/MERLIN.md`) for an agent that
  wants the operating model in one read; nothing injects it.
- **Hooks**: the agent-state hooks (`terminal/hooks/`) installed in
  `~/.claude/settings.json` and `~/.codex/hooks.json` under the
  `AGENT_STATE_HOOKS` consent (`lib/skills.py` reconcilers). They stamp the
  window's state and bind its conversation, and they are how a job run's
  finish is known.
- **The `merlin` CLI**: every capability is a subcommand that works from any
  directory (`merlin job`, `merlin skills`, `merlin config`, the
  extension commands).

## A job run

```
scheduler tick (every minute, job/runner.py)         webhook fire (in-process)
  └─ retention.tick(): finalize orphans, close          └─ job/webhook.py: per-job
     windows past retention                                lock, coalesce, adopt
  └─ for each due job, under its flock:
       runs.new_run()  -> a run directory (meta.json, the prompt, the log)
       runs.launch()   -> a window in merlin-jobs, @job_* options stamped,
                          run.sh runs the agent or the command foreground
       supervisor.supervise() -> watches the window: done, or busy then
                          idle, or the exit file, or the deadline (124),
                          or the window gone (130)
       runner.record_run() -> outcome.json, then history and the run log,
                          then result.json (exactly once, resumable)
```

The window outlives the run: a succeeded, untouched window closes after an
hour or at the job's next run; a failed one, or one the user stepped into,
stays a day after the last activity; a window someone is looking at is
never closed. A run whose Merlin restarted underneath it is adopted at the
next tick. Full reference: [`job-system.md`](job-system.md).

## Notifications

`notifications/watcher.py` sweeps the windows every two seconds and turns
state transitions into events: a plain window's `done` or `ask`, a job
run's finish decided once by the job's `report_mode` (`always`, `silent`,
`off`) from the `@job_*` options, with the failure wording and the exit
code. Events ride `/api/board?since=` to the page and go to Web Push for
subscribed devices (`notifications/push.py`). Full reference:
[`notifications.md`](notifications.md).

## Path Resolution

```
┌─────────────────────────────────────────────────────────────────────┐
│                         paths.py                                     │
│              (All modules import paths for file resolution)          │
│                                                                     │
│  Dev mode detection:                                                │
│    1. Explicit set_dev_mode() call (--dev flag)                     │
│    2. MERLIN_DEV=1 env var                                          │
│    3. .git/ directory in paths.py parent                            │
│                                                                     │
│  Dev mode (git checkout):        Installed mode (~/.merlin/):       │
│  ┌─────────────────────────┐    ┌──────────────────────────────┐   │
│  │ app_dir → repo root     │    │ app_dir → ~/.merlin/current/ │   │
│  │ data_dir → ~/.merlin/   │    │ data_dir → ~/.merlin/        │   │
│  │ config → config.env     │    │ config → config.env          │   │
│  │ jobs → ~/.merlin/jobs/  │    │ jobs → ~/.merlin/jobs/       │   │
│  └─────────────────────────┘    └──────────────────────────────┘   │
│                                                                     │
│  MERLIN_HOME overrides ~/.merlin/; the notes extension resolves its │
│  own directory (NOTES_DIR) when it is enabled                       │
└─────────────────────────────────────────────────────────────────────┘
```

## Startup Flow

```
cli.py (merlin start)
  │
  ├── set_dev_mode() if --dev
  ├── Check for config.env → run_setup() if missing
  │
  └── main.start_server(port, host)
        │
        ├── structured_log.cleanup_old_logs()  (engine-log retention)
        ├── _validate_config()
        │     ├── Check DASHBOARD_PASS (warn if empty)
        │     └── _check_optional_deps()
        │           └── tmux missing → TMUX_AVAILABLE=False, disable nav item
        │
        ├── _rebuild_skill_registry()  (aggregate ~/.merlin/skills, refresh
        │     the ~/.claude, ~/.agents and $CODEX_HOME shims, reconcile the
        │     agent-state hooks per consent — see skill-system.md)
        │
        └── asyncio.run()
              ├── uvicorn.Server (FastAPI app)
              ├── start_saas_tunnel() + SSH server (SaaS mode only)
              ├── job.start() (the scheduler), the attention watcher,
              │   the workspace snapshot service
              ├── Timeline flag off: retire its provider hooks if present
              └── extension.start() for each extension with start() hook
```

## Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, startup, extension loader, settings API |
| `cli.py` | CLI entry point and subcommands (`start`, `version`, `setup`, `update`, `config`, `skills`, `agent`, `job`, `review`, `dashboard-url`) |
| `paths.py` | Path resolution (dev mode vs installed) |
| `lib/skills.py` | Skill registry, canonical aggregation, shims, the agent-state hook reconcilers (see [`skill-system.md`](skill-system.md)) |
| `lib/structured_log.py`, `lib/event_log.py` | The engine log writer and its typed reader (see [`logging-system.md`](logging-system.md)) |
| `terminal/` | The web terminal: PTY bridge, tmux, the agent hooks (`terminal/hooks/`), dictation (`terminal/transcribe.py`) |
| `board/` | The tmux sweep (`board/sweep.py`) and the Sessions board |
| `notifications/` | The attention watcher, the event content, Web Push |
| `job/agents.py` | The agent table: launch lines, the bypass warning, launch problems |
| `job/runs.py` | A run: its directory, its window, `run.sh` |
| `job/supervisor.py` | Watching a run to its finish; ending one |
| `job/retention.py` | Orphans finalized, windows closed by the retention rules |
| `job/runner.py` | Job dispatcher (due jobs, the record), spawned every minute |
| `job/webhook.py` | The webhook fire, single-flight per job |
| `job/__init__.py` | Scheduler loop |
| `workspace/` | Continuous tmux snapshot and the restore after a reboot |
| `voice/` | Voice mode |
| `notes/`, `timeline/` | Built-in extensions (notes off by default; the Timeline behind its flag) |
