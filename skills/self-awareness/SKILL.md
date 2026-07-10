---
name: self-awareness
description: Understand your own architecture, source code, configuration, and runtime behavior. Use when you need to inspect how you work, review your logs, debug your own behavior, or answer questions about your capabilities.
user-invocable: false
allowed-tools: Bash, Read, Glob, Grep
---

# Self-Awareness Skill

You can introspect on your own architecture, configuration, and runtime behavior.

## Where You're Documented

- **`agent/MERLIN.md`** — Your operating guide (the agent brain doc); print it with `merlin agent`
- **`CLAUDE.md`** (project root) — Development doc: project architecture, script inventory, data flow, logging, conventions
- Every script: `uv run <script>.py --help`

## Project Structure

Resolve the source root first: `APP=$(merlin config app-dir)`. All paths
below are relative to it (e.g. `cat "$APP/agent/MERLIN.md"`, or just run
`merlin agent`).

Your source code spans several directories from the project root:

- **`main.py`**, **`cli.py`**, **`auth.py`**, **`tunnel.py`**, **`paths.py`** — Core entry points and utilities
- **`lib/`** — Shared libraries (`engine.py`, `claude.py`, `session.py`, `structured_log.py`)
- **`job/`** — Job core module (`runner.py`, `manage.py`, `state.py`, `logs.py`, `routes.py`, `notify.py`)
- **`merlin-bot/`** — Discord bot extension (`merlin_bot.py`, `discord_send.py` transport, `merlin_app.py`)
- **`files/`**, **`terminal/`**, **`commits/`**, **`notes/`** — Dashboard modules

## What You Can Inspect

- **Your source code** — read any `.py` file in the project root, `lib/`, `job/`, or `merlin-bot/`
- **Your skills** — core skills ship in `skills/*/SKILL.md`; bot-gated skills in `merlin-bot/skills/*/SKILL.md`; personal skills in `$(merlin config skills-user-dir)/*/SKILL.md`. All are aggregated into `~/.merlin/skills/`
- **Your jobs** — `merlin job list` and `~/.merlin/jobs/*.json`
- **Your notes** — `$(merlin config notes-dir)/user.md`, `kb/`, `logs/`
- **Your config** — `~/.merlin/config.env`

## Your Logs

All logs live under `~/.merlin/logs/`.

### Engine log (`~/.merlin/logs/engine-log.jsonl`)

Single JSONL file, one event per line. Event types: `invocation`, `bot_event`, `job_dispatch`, `app_start`, `app_stop`. Source of truth for the monitoring dashboard. Includes `stderr`, `request_id` for correlation.

```bash
tail -20 ~/.merlin/logs/engine-log.jsonl | python3 -m json.tool --no-ensure-ascii
grep '"error"' ~/.merlin/logs/engine-log.jsonl | tail -10
```

### App log (`~/.merlin/logs/merlin.log`)

Unified app log — all modules (bot, jobs, core) use the `merlin.*` logger hierarchy. `RotatingFileHandler`, 10 MB × 5.

```bash
tail -100 ~/.merlin/logs/merlin.log
```

### Raw sessions (`~/.merlin/logs/raw-sessions/`)

Raw engine output per invocation (stream-json). Powers the session viewer in the dashboard.

```bash
ls -lt ~/.merlin/logs/raw-sessions/sessions/ | head -10
```
