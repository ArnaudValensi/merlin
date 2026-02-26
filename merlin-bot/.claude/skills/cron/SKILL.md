---
name: cron
description: Manage scheduled cron jobs — add, list, edit, enable/disable, remove jobs and view run history.
user-invocable: false
allowed-tools: Bash
---

# Cron Jobs Skill

Manage scheduled tasks using `cron_manage.py`. All operations use this script for validation and consistent formatting.

## Commands

All commands run from `merlin-bot/` directory.

### List Jobs

```bash
uv run cron_manage.py list --discord
```

Send the output directly to Discord. Example output:
```
**Cron jobs (2 active, 1 disabled)**
1. ✅ **daily-python-check** — daily at 9:00 — silent
2. ✅ **weekly-pr-review** — Mondays at 8:00 — always
3. ⏸️ **old-task** — daily at 12:00 (disabled)
```

### Get Job Details

```bash
uv run cron_manage.py get <job-id> --discord
```

### Add Job

**Conversational flow:**
1. Ask what the job should do
2. Ask how often (translate to cron expression)
3. Ask if it should always report or only when there's news

**Preview with dry-run:**
```bash
uv run cron_manage.py add \
  --schedule "0 9 * * *" \
  --prompt "Check for new Python releases" \
  --channel <channel_id> \
  --description "Daily Python check" \
  --report-mode silent \
  --dry-run
```

Show the user a summary and ask for confirmation. After confirmation, run without `--dry-run`:

```bash
uv run cron_manage.py add \
  --schedule "0 9 * * *" \
  --prompt "Check for new Python releases" \
  --channel <channel_id> \
  --description "Daily Python check" \
  --report-mode silent
```

**Options:**
- `--schedule` — Cron expression (required)
- `--prompt` — Task for Claude to execute (required)
- `--channel` — Discord channel ID (required)
- `--description` — Human-readable summary (used to generate job ID)
- `--id` — Explicit job ID (optional, auto-generated from description)
- `--report-mode` — `always` (default) or `silent`
- `--max-turns` — Max agentic turns, 0 = unlimited (default: 0)

### Enable/Disable Job

```bash
uv run cron_manage.py enable <job-id>
uv run cron_manage.py disable <job-id>
```

Confirm the action to the user after running.

### Remove Job

**Always confirm before removing:**
1. Ask: "Delete **job-id**? This cannot be undone."
2. Only after confirmation:

```bash
uv run cron_manage.py remove <job-id>
```

### Run Job Now

Run a job immediately, bypassing the schedule:

```bash
uv run cron_runner.py --job <job-id>
```

This executes the job exactly like the scheduler would — same logging, history tracking, and session handling. Useful for:
- Testing a new job
- Retrying a failed job
- Running on demand

Example:
```bash
uv run cron_runner.py --job daily-python-check
```

After running, confirm completion to the user (e.g., "Running **daily-python-check** now — check the thread for results").

### Run History

```bash
uv run cron_manage.py history <job-id> --discord
```

Or for all jobs:
```bash
uv run cron_manage.py history --discord
```

Example output:
```
**Recent runs: daily-python-check**
• 2026-02-04 09:00 — ✅ success (12.3s)
• 2026-02-03 09:00 — ✅ success (8.1s)
• 2026-02-02 09:00 — ❌ failed (2.0s)
```

## Cron Expression Cheat Sheet

| Schedule | Expression |
|----------|------------|
| Every minute | `* * * * *` |
| Every hour | `0 * * * *` |
| Daily at 9:00 | `0 9 * * *` |
| Monday at 9:00 | `0 9 * * 1` |
| Weekdays at 8:00 | `0 8 * * 1-5` |
| Twice daily (9:00, 18:00) | `0 9,18 * * *` |
| Every 2 hours | `0 */2 * * *` |
| First of month | `0 0 1 * *` |
| Sunday at midnight | `0 0 * * 0` |

**Format**: `minute hour day-of-month month day-of-week`

**Translations:**
- "every morning" → `0 9 * * *`
- "every hour" → `0 * * * *`
- "twice a day" → `0 9,18 * * *`
- "every Monday" → `0 9 * * 1`
- "weekdays at 8am" → `0 8 * * 1-5`

## Tips

- Always use `--discord` flag when displaying to users for consistent formatting
- Use `--dry-run` to preview job creation before confirming
- Use `report_mode: silent` for monitoring (only notifies when something found)
- Use `report_mode: always` for status reports (always sends update)
- The script validates cron expressions — invalid ones will error
