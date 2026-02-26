# Cron Jobs — Tasks

## T1: Job file format and directory
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: —
- **Description**: Create `merlin-bot/cron-jobs/` directory (gitignored contents, but keep the dir). Define and document the JSON schema for job files. Create a sample job file for reference (e.g. `_example.json.template`).

## T2: State and history tracking
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: —
- **Description**: Implement helper functions for:
  - `.state.json`: read/write last run timestamp per job ID
  - `.history.json`: append run results, read history, rolling limit of 100 per job
  - These will be used by the dispatcher and the cron skill

## T3: Dispatcher (`cron_runner.py`)
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T1, T2
- **Description**: Create `merlin-bot/cron_runner.py` (PEP 723, `uv run`). Implement:
  - Read all `*.json` files from `cron-jobs/` (skip dotfiles and templates)
  - For each enabled job, check if due using `croniter` and last run time from `.state.json`
  - If due: build prompt (append report_mode instruction), call `invoke_claude()` with deterministic session ID, log result, update state and history
  - For `report_mode: "silent"`, append: "Only send a message to Discord if you have something noteworthy to report. If nothing to report, do nothing."
  - For `report_mode: "always"`, append: "Send your findings to Discord even if there's nothing new."
  - Logging to `logs/cron_runner.log`
  - Handle errors gracefully (one failing job doesn't stop others)

## T4: Cron skill (SKILL.md)
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T1
- **Description**: Create `merlin-bot/.claude/skills/cron/SKILL.md`. The skill defines how Merlin manages cron jobs with the following operations:

  **Add job** — conversational flow:
  1. Merlin asks questions to collect all required info:
     - What should the job do? (→ prompt)
     - How often? (→ schedule, Merlin translates to cron expression)
     - Should it always report or only when there's something to say? (→ report_mode)
  2. Merlin presents a formatted summary for confirmation:
     ```
     **New cron job**
     **Name**: check-python-releases
     **Schedule**: every day at 9:00 (0 9 * * *)
     **Report mode**: silent (only reports if something found)
     **Task**: Check for new Python releases
     ```
  3. Only after user confirms: create the job file

  **Edit job** — same conversational flow as add, but shows current values and asks what to change, then confirms before saving

  **List jobs** — formatted overview of all jobs:
  ```
  **Cron jobs (3 active, 1 disabled)**
  1. ✅ check-python-releases — daily at 9:00 — silent
  2. ✅ weekly-pr-review — Mon at 8:00 — always
  3. ✅ server-health — every hour — silent
  4. ⏸️ old-task — daily at 12:00 — always (disabled)
  ```

  **Enable/disable job** — toggle with confirmation

  **Remove job** — confirm before deleting

  **Run history** — show recent runs for a specific job or all jobs:
  ```
  **Recent runs: check-python-releases**
  • 2026-02-04 09:00 — ✅ success (12.3s)
  • 2026-02-03 09:00 — ✅ success (8.1s)
  • 2026-02-02 09:00 — ❌ failed (2.0s)
  ```

  Also include: common cron expressions cheat sheet for Merlin's reference

## T5: Unit tests
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T2, T3
- **Description**: Write pytest tests for the dispatcher and state/history helpers:
  - Job loading: valid jobs, invalid JSON, disabled jobs, missing fields
  - Schedule checking: job due, job not due, first run (no state)
  - Prompt building: report_mode "always" vs "silent" appended instructions
  - Session ID: deterministic from job filename
  - State update: last run timestamp written after execution
  - History: append, rolling limit, read by job ID
  - Error handling: one bad job doesn't crash the dispatcher

## T6: Crontab setup and docs
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T3
- **Description**: Add the crontab entry for the dispatcher. Update `merlin/CLAUDE.md` with cron documentation. Document:
  - How to install the crontab entry
  - How to verify it's running
  - How to check logs

## T7: Integration test
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T3, T4, T5
- **Description**: End-to-end test:
  - Create a test job file (runs every minute, simple prompt)
  - Run the dispatcher manually, verify it executes the job
  - Verify state and history are updated
  - Verify logs are written
  - Ask Merlin via Discord to add a cron job, verify the full flow (questions → summary → confirmation → file created)
  - Ask Merlin to list jobs, enable/disable, show history
  - Document results in journal entry
