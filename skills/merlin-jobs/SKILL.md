---
name: merlin-jobs
description: Manage Merlin jobs — add, list, edit, enable/disable, remove scheduled jobs and view run history.
user-invocable: false
allowed-tools: Bash
---

# Jobs Skill

Manage jobs using the `merlin job` command. All operations use
this command for validation and consistent formatting. It works from any
directory.

**IMPORTANT: Never read or write `jobs/*.json` files directly.** Always
use the `merlin job` commands below. The command handles path resolution,
validation, and formatting. Reading files directly wastes cycles on path
guessing; writing directly bypasses validation and can create broken jobs.

## Commands

### List Jobs

```bash
merlin job list
```

### Get Job Details

```bash
merlin job get <job-id>
```

### Add Job

**Conversational flow:**
1. Ask what the job should do
2. Ask how often (translate to cron expression)
3. Ask if it should always report or only when there's news (report_mode)

**Preview with dry-run:**
```bash
merlin job add \
  --schedule "0 9 * * *" \
  --prompt "Check for new Python releases" \
  --description "Daily Python check" \
  --report-mode silent \
  --dry-run
```

Show the user a summary and ask for confirmation. After confirmation, run without `--dry-run`:

```bash
merlin job add \
  --schedule "0 9 * * *" \
  --prompt "Check for new Python releases" \
  --description "Daily Python check" \
  --report-mode silent
```

**Important:** The prompt should describe the task, as if typed into the
agent. Every run opens the agent's real interface in a window of the
`merlin-jobs` tmux session, with every permission check bypassed (the command
prints that warning on stderr): tell the user a job only gets work they would
let run unattended.

**Options:**
- `--schedule` — Cron expression (optional; omit for a webhook- or manual-only job)
- `--prompt` — The prompt the agent runs, or `--command` — a shell command instead (one of the two is required)
- `--working-dir` — Where the run starts. A Codex job needs a directory Codex trusts
- `--keep-window` — Leave each run's window open until the user closes it (each run adds one)
- `--agent` — `claude` (default) or `codex`
- `--webhook` — Also enable the webhook trigger (generates a secret)

For all other flags (report mode, description, explicit id) run `merlin job add --help`; never restate them from memory.

### Enable/Disable Job

```bash
merlin job enable <job-id>
merlin job disable <job-id>
```

Confirm the action to the user after running.

### Remove Job

**Always confirm before removing:**
1. Ask: "Delete **job-id**? This cannot be undone."
2. Only after confirmation:

```bash
merlin job remove <job-id>
```

### Run Job Now

Run a job immediately, bypassing the schedule:

```bash
merlin job trigger <job-id>
```

This executes the job exactly like the scheduler would (same window, logging
and history), waits for the run to finish and prints its exit code, run id
and window. Useful for:
- Testing a new job
- Retrying a failed job
- Running on demand

### Webhook Trigger

A job can be fired by an external HTTP call (secret-gated). Enable, inspect,
test, and rotate:

```bash
merlin job webhook <job-id> --enable    # generate secret, turn the trigger on
merlin job url <job-id>                 # print public URL + secret + ready curl
merlin job test <job-id>                # fire the hook against the local server
merlin job webhook <job-id> --rotate    # replace the secret
merlin job webhook <job-id> --disable   # remove the trigger
```

One run at a time per job: extra fires while a run is active coalesce into
it. **Treat the secret as a credential** — only show it when the user asks
for it.

### Run History

```bash
merlin job history <job-id>
```

Or for all jobs:
```bash
merlin job history
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
- Report modes (Merlin notifications from the run's window): `always`
  (digests, reports), `silent` (failures and questions only, good for
  monitoring jobs), `off` (nothing from the run); see `merlin job add --help`
- The command validates cron expressions — invalid ones will error
