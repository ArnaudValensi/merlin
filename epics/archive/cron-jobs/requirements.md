# Cron Jobs — Requirements

## Goal

Allow Merlin to create, manage, and execute scheduled tasks (cron jobs) based on user requests in Discord. Users ask Merlin to do something recurring, Merlin creates a job, and a dispatcher runs it on schedule.

## Context

The wrapper (`claude_wrapper.py`), Discord skill, and bot listener (`merlin.py`) are already built. This epic adds the cron system: a jobs directory, a dispatcher, a cron skill for Merlin, and run history tracking.

## Requirements

### R1: Jobs directory
- **Status**: `accepted`
- Jobs live in `merlin-bot/cron-jobs/`, one JSON file per job
- Each job file contains:
  - `schedule`: cron expression (e.g. `0 9 * * *`)
  - `prompt`: the prompt to send to Claude
  - `channel`: Discord channel ID to report to
  - `enabled`: boolean (allows disabling without deleting)
  - `description`: human-readable summary
  - `report_mode`: `"always"` (always send result to Discord) or `"silent"` (only report if there's something to say — Claude decides)
  - `max_turns`: max agentic turns per execution (default: 20, safety limit)
  - `created_at`: ISO timestamp
- Job filename is the job ID (e.g. `check-python-releases.json`)

### R2: Dispatcher (`cron_runner.py`)
- **Status**: `accepted`
- PEP 723 script, run with `uv run cron_runner.py`
- Runs every minute via a single crontab entry: `* * * * * cd /path/to/merlin-bot && uv run cron_runner.py`
- On each run:
  - Read all job files from `cron-jobs/`
  - For each enabled job, check if it's due (based on schedule and last run time)
  - If due, call `invoke_claude()` with the job's prompt, channel, and max_turns
  - For `report_mode: "silent"`, append instruction to the prompt telling Claude to only report if there's something noteworthy
  - For `report_mode: "always"`, append instruction to always send a result to Discord
  - Log execution result
- Track last run time per job (in a state file `cron-jobs/.state.json`)
- Each job gets a deterministic session ID (uuid5 from job filename) for memory across runs
- Use `--resume` first, fall back to `--session-id` (same pattern as merlin.py)

### R3: Run history
- **Status**: `accepted`
- Track run history per job in a history file: `cron-jobs/.history.json`
- Each entry: `{job_id, timestamp, exit_code, duration, session_id}`
- Keep last 100 runs per job (rolling)
- The cron skill can display this history in Discord on request

### R4: Cron skill for Merlin
- **Status**: `accepted`
- A Claude Code skill (`merlin-bot/.claude/skills/cron/SKILL.md`) that tells Merlin how to manage cron jobs
- Operations:
  - **Add**: create a new job file in `cron-jobs/` (Merlin uses the Write tool)
  - **List**: read all job files and summarize them
  - **Remove**: delete a job file
  - **Enable/disable**: toggle the `enabled` field
  - **History**: read `.history.json` and format recent runs for Discord
- The skill should guide Merlin on:
  - How to derive a good filename from the description
  - The JSON format for job files
  - How to translate natural language schedules to cron expressions
  - How to set `report_mode` based on the user's intent

### R5: Crontab setup
- **Status**: `accepted`
- A single crontab entry to run the dispatcher every minute
- Document the setup in CLAUDE.md
- The dispatcher is lightweight (just reads files and checks times) — running every minute is fine

### R6: Logging
- **Status**: `accepted`
- The dispatcher logs to `merlin-bot/logs/cron_runner.log`
- Each job execution is also logged by the wrapper (existing behavior)
- Log: job started, job skipped (not due / disabled), job completed (exit code, duration)

## Out of Scope

- Web UI for managing jobs (future)
- Job dependencies / chaining (future)
- Sub-minute scheduling (future)
- Cost tracking per job (future — could be added via usage field in history)
