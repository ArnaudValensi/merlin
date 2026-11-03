---
name: merlin
description: How Merlin works under the hood. Its architecture, source code, configuration, skills, jobs and logs. Use when the user asks how Merlin works, wants its logs or source inspected, debugs a job or a dashboard behavior, or asks what Merlin can do.
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
- **`lib/`** — Shared libraries (`skills.py`, `structured_log.py`, `event_log.py`)
- **`job/`** — Job core module (`runner.py`, `manage.py`, `state.py`, `logs.py`, `routes.py`, `notify.py`)
- **`files/`**, **`terminal/`**, **`commits/`**, **`notes/`** — Dashboard modules
- **`timeline/`** — Built-in activity-history extension and provider hook normalizer

## What You Can Inspect

- **Merlin's jobs** — `merlin job list` and `~/.merlin/jobs/*.json`
- **Merlin's notes** — `$(merlin config notes-dir)/user.md`, `kb/`, `logs/`
- **Merlin's config** — `~/.merlin/config.env`

## Merlin's Logs

All logs live under `~/.merlin/logs/`.

### Event log (`~/.merlin/logs/events.jsonl`)

Single JSONL file, one event per line. Event types: `app_started`, `app_stopped`, `job_runner_crash`, `webhook_request`, `attention_routed`. The jobs page reads the crashes and the webhook requests from it.

```bash
tail -20 ~/.merlin/logs/events.jsonl | python3 -m json.tool --no-ensure-ascii
grep '"error"' ~/.merlin/logs/events.jsonl | tail -10
```

### App log (`~/.merlin/logs/merlin.log`)

Unified app log — all modules use the `merlin.*` logger hierarchy. `RotatingFileHandler`, 10 MB × 5.

```bash
tail -100 ~/.merlin/logs/merlin.log
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
