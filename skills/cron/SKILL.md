---
name: cron
description: Manage scheduled cron jobs — add, list, edit, enable/disable, remove jobs and view run history.
user-invocable: false
allowed-tools: Bash
---

# Cron Jobs Skill

Manage scheduled tasks using the `merlin cron` command. All operations use
this command for validation and consistent formatting. It works from any
directory.

**IMPORTANT: Never read or write `cron-jobs/*.json` files directly.** Always
use the `merlin cron` commands below. The command handles path resolution,
validation, and formatting. Reading files directly wastes cycles on path
guessing; writing directly bypasses validation and can create broken jobs.

## Commands

### List Jobs

```bash
merlin cron list
```

### Get Job Details

```bash
merlin cron get <job-id>
```

### Add Job

**Conversational flow:**
1. Ask what the job should do
2. Ask how often (translate to cron expression)
3. Ask if it should always report or only when there's news (report_mode)

**Preview with dry-run:**
```bash
merlin cron add \
  --schedule "0 9 * * *" \
  --prompt "Check for new Python releases" \
  --description "Daily Python check" \
  --report-mode silent \
  --dry-run
```

Show the user a summary and ask for confirmation. After confirmation, run without `--dry-run`:

```bash
merlin cron add \
  --schedule "0 9 * * *" \
  --prompt "Check for new Python releases" \
  --description "Daily Python check" \
  --report-mode silent
```

**Important:** The prompt should describe the task, not how to deliver results. The engine returns text output and the notification system handles delivery. Never include "send to Discord" or similar in prompts.

**Options:**
- `--schedule` — Cron expression (required)
- `--prompt` — Task for the engine to execute (required)

For all other flags (report mode, max turns, description, explicit id,
channel routing) run `merlin cron add --help`; never restate them from
memory.

### Enable/Disable Job

```bash
merlin cron enable <job-id>
merlin cron disable <job-id>
```

Confirm the action to the user after running.

### Remove Job

**Always confirm before removing:**
1. Ask: "Delete **job-id**? This cannot be undone."
2. Only after confirmation:

```bash
merlin cron remove <job-id>
```

### Run Job Now

Run a job immediately, bypassing the schedule:

```bash
merlin cron trigger <job-id>
```

This executes the job exactly like the scheduler would — same logging, history tracking, and session handling. Useful for:
- Testing a new job
- Retrying a failed job
- Running on demand

### Run History

```bash
merlin cron history <job-id>
```

Or for all jobs:
```bash
merlin cron history
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

- Use `--dry-run` to preview job creation before confirming
- Report modes: `always` (digests, reports), `silent` (errors only, good
  for monitoring jobs), `off` (no notifications); see `merlin cron add --help`
- The command validates cron expressions — invalid ones will error
