---
name: merlin
description: How Merlin works under the hood. Its architecture, source code, configuration, skills, jobs and logs. Use when the user asks how Merlin works, wants its logs or source inspected, debugs a job, a channel or a dashboard behavior, or asks what Merlin can do.
user-invocable: false
allowed-tools: Bash, Read, Glob, Grep
---

# Merlin Skill

Merlin is the suite of tools you operate: one process on the user's machine
with a web dashboard, chat integrations, scheduled jobs and a shared notes
memory. This skill is how you inspect Merlin itself: its code, its
configuration and its runtime behavior.

## Where Merlin Is Documented

- **`agent/MERLIN.md`** — The operating guide for the agent (the brain doc); print it with `merlin agent`
- **`CLAUDE.md`** (project root) — Development doc: project architecture, script inventory, data flow, logging, conventions
- Every script: `uv run <script>.py --help`

## Project Structure

Resolve the source root first: `APP=$(merlin config app-dir)`. All paths
below are relative to it (e.g. `cat "$APP/agent/MERLIN.md"`, or just run
`merlin agent`).

Merlin's source code spans several directories from the project root:

- **`main.py`**, **`cli.py`**, **`auth.py`**, **`tunnel.py`**, **`paths.py`** — Core entry points and utilities
- **`lib/`** — Shared libraries (`engine.py`, `claude.py`, `session.py`, `structured_log.py`)
- **`job/`** — Job core module (`runner.py`, `manage.py`, `state.py`, `logs.py`, `routes.py`, `notify.py`)
- **`merlin-bot/`** — Discord bot extension (`merlin_bot.py`, `discord_send.py` transport, `merlin_app.py`)
- **`files/`**, **`terminal/`**, **`commits/`**, **`notes/`** — Dashboard modules
- **`timeline/`** — Built-in activity-history extension and provider hook normalizer

## What You Can Inspect

- **Merlin's source code** — read any `.py` file in the project root, `lib/`, `job/`, or `merlin-bot/`
- **Merlin's skills** — core skills ship in `skills/*/SKILL.md`; bot-gated skills in `merlin-bot/skills/*/SKILL.md`; personal skills in `$(merlin config skills-user-dir)/*/SKILL.md`. All are aggregated into `~/.merlin/skills/`
- **Merlin's jobs** — `merlin job list` and `~/.merlin/jobs/*.json`
- **Merlin's notes** — `$(merlin config notes-dir)/user.md`, `kb/`, `logs/`
- **Merlin's config** — `~/.merlin/config.env`

## Merlin's Logs

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

### Activity Timeline (`~/.merlin/logs/activity/`)

Sanitized Codex, Claude Code, and explicit workflow lifecycle metadata. Daily
JSONL partitions contain event/span boundaries and provider context, never raw
prompts, commands, tool input/results, or model output. Use the authenticated
`/timeline` page for the assembled participant view; inspect the JSONL only when
debugging capture or correlation.

```bash
ls -lt ~/.merlin/logs/activity/*.jsonl | head -10
tail -20 ~/.merlin/logs/activity/$(date -u +%F).jsonl
```
