# /// script
# dependencies = [
#   "croniter",
# ]
# ///
"""
Cron job management script — create, list, edit, enable/disable, remove jobs.

Used by the cron skill to ensure reliable, validated operations.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # project root for paths module

from croniter import croniter

import paths
from cron_state import get_all_history, get_history

CRON_JOBS_DIR = paths.cron_jobs_dir()

# Required fields for a valid job
REQUIRED_FIELDS = ["schedule", "prompt", "channel"]

# Default values for optional fields
DEFAULTS = {
    "enabled": True,
    "report_mode": "always",
    "max_turns": 0,
}


# ---------------------------------------------------------------------------
# Cron expression helpers
# ---------------------------------------------------------------------------


def validate_cron(expression: str) -> bool:
    """Check if a cron expression is valid."""
    try:
        croniter(expression)
        return True
    except (KeyError, ValueError):
        return False


def cron_to_human(expression: str) -> str:
    """Convert a cron expression to human-readable format."""
    parts = expression.split()
    if len(parts) != 5:
        return expression

    minute, hour, dom, month, dow = parts

    # Common patterns
    if expression == "* * * * *":
        return "every minute"
    if minute == "0" and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return "every hour"
    if minute == "0" and hour.startswith("*/") and dom == "*" and month == "*" and dow == "*":
        interval = hour[2:]
        return f"every {interval} hours"
    if minute == "0" and dom == "*" and month == "*" and dow == "*":
        return f"daily at {hour}:00"
    if minute != "*" and hour != "*" and dom == "*" and month == "*" and dow == "*":
        return f"daily at {hour}:{minute.zfill(2)}"
    if minute == "0" and dom == "*" and month == "*" and dow == "0":
        return f"Sundays at {hour}:00"
    if minute == "0" and dom == "*" and month == "*" and dow == "1":
        return f"Mondays at {hour}:00"
    if minute == "0" and dom == "*" and month == "*" and dow == "1-5":
        return f"weekdays at {hour}:00"
    if minute == "0" and dom == "1" and month == "*" and dow == "*":
        return f"1st of month at {hour}:00"
    if minute.startswith("*/"):
        interval = minute[2:]
        return f"every {interval} minutes"

    # Day of week names
    dow_names = {
        "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
        "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun",
    }
    if dow in dow_names and minute == "0" and dom == "*" and month == "*":
        return f"{dow_names[dow]} at {hour}:00"

    return expression  # Fall back to raw expression


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")[:50]


# ---------------------------------------------------------------------------
# Job operations
# ---------------------------------------------------------------------------


def load_job(job_id: str) -> dict | None:
    """Load a job by ID. Returns None if not found or invalid."""
    path = CRON_JOBS_DIR / f"{job_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def save_job(job_id: str, job: dict) -> Path:
    """Save a job to disk. Returns the path."""
    CRON_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = CRON_JOBS_DIR / f"{job_id}.json"
    path.write_text(json.dumps(job, indent=2))
    return path


def list_jobs() -> list[dict]:
    """List all jobs with their IDs."""
    jobs = []
    if not CRON_JOBS_DIR.exists():
        return jobs

    for path in sorted(CRON_JOBS_DIR.glob("*.json")):
        if path.name.startswith(".") or path.name.startswith("_"):
            continue
        try:
            job = json.loads(path.read_text())
            job["id"] = path.stem
            jobs.append(job)
        except json.JSONDecodeError:
            continue

    return jobs


def delete_job(job_id: str) -> bool:
    """Delete a job. Returns True if deleted, False if not found."""
    path = CRON_JOBS_DIR / f"{job_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Discord formatting
# ---------------------------------------------------------------------------


def format_jobs_discord(jobs: list[dict]) -> str:
    """Format job list for Discord."""
    if not jobs:
        return "**No cron jobs configured.**"

    active = sum(1 for j in jobs if j.get("enabled", True))
    disabled = len(jobs) - active

    lines = []
    if disabled > 0:
        lines.append(f"**Cron jobs ({active} active, {disabled} disabled)**")
    else:
        lines.append(f"**Cron jobs ({active} active)**")

    for i, job in enumerate(jobs, 1):
        enabled = job.get("enabled", True)
        icon = "✅" if enabled else "⏸️"
        schedule_human = cron_to_human(job.get("schedule", ""))
        report_mode = job.get("report_mode", "always")
        job_id = job.get("id", "unknown")

        suffix = ""
        if not enabled:
            suffix = " (disabled)"
        elif report_mode == "silent":
            suffix = " — silent"
        elif report_mode == "always":
            suffix = " — always"

        lines.append(f"{i}. {icon} **{job_id}** — {schedule_human}{suffix}")

    return "\n".join(lines)


def format_job_discord(job: dict, job_id: str) -> str:
    """Format a single job for Discord."""
    enabled = job.get("enabled", True)
    status = "enabled" if enabled else "disabled"
    schedule = job.get("schedule", "")
    schedule_human = cron_to_human(schedule)
    report_mode = job.get("report_mode", "always")
    max_turns = job.get("max_turns", 0)
    description = job.get("description", "No description")
    prompt = job.get("prompt", "")
    channel = job.get("channel", "")

    # Truncate prompt if too long
    prompt_display = prompt[:200] + "..." if len(prompt) > 200 else prompt

    lines = [
        f"**Job: {job_id}**",
        f"**Status**: {status}",
        f"**Schedule**: {schedule_human} (`{schedule}`)",
        f"**Report mode**: {report_mode}",
        f"**Max turns**: {'unlimited' if max_turns == 0 else max_turns}",
        f"**Channel**: {channel}",
        f"**Description**: {description}",
        f"**Prompt**: {prompt_display}",
    ]

    return "\n".join(lines)


def format_history_discord(job_id: str, runs: list[dict]) -> str:
    """Format run history for Discord."""
    if not runs:
        return f"**No run history for {job_id}**"

    lines = [f"**Recent runs: {job_id}**"]

    for run in runs[:10]:  # Limit to 10 most recent
        ts = run.get("timestamp", "")
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                ts_display = dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                ts_display = ts[:16]
        else:
            ts_display = "unknown"

        exit_code = run.get("exit_code", -1)
        duration = run.get("duration", 0)
        icon = "✅" if exit_code == 0 else "❌"
        status = "success" if exit_code == 0 else f"failed (exit {exit_code})"

        lines.append(f"• {ts_display} — {icon} {status} ({duration:.1f}s)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_add(args) -> dict:
    """Add a new cron job."""
    # Validate cron expression
    if not validate_cron(args.schedule):
        return {"ok": False, "error": f"Invalid cron expression: {args.schedule}"}

    # Generate or validate job ID
    if args.id:
        job_id = slugify(args.id)
    elif args.description:
        job_id = slugify(args.description)
    else:
        return {"ok": False, "error": "Either --id or --description is required"}

    if not job_id:
        return {"ok": False, "error": "Could not generate valid job ID"}

    # Check if job already exists
    if load_job(job_id) is not None:
        return {"ok": False, "error": f"Job '{job_id}' already exists"}

    # Build job
    job = {
        "description": args.description or job_id,
        "schedule": args.schedule,
        "prompt": args.prompt,
        "channel": args.channel,
        "enabled": True,
        "report_mode": args.report_mode,
        "max_turns": args.max_turns,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "job_id": job_id,
            "job": job,
            "message": f"Would create job '{job_id}'",
        }

    # Save job
    path = save_job(job_id, job)

    return {
        "ok": True,
        "job_id": job_id,
        "path": str(path),
        "message": f"Created job '{job_id}'",
    }


def cmd_list(args) -> dict | str:
    """List all cron jobs."""
    jobs = list_jobs()

    if args.discord:
        return format_jobs_discord(jobs)

    return {"ok": True, "jobs": jobs, "count": len(jobs)}


def cmd_get(args) -> dict | str:
    """Get a single job's details."""
    job = load_job(args.job_id)

    if job is None:
        if args.discord:
            return f"**Job not found: {args.job_id}**"
        return {"ok": False, "error": f"Job not found: {args.job_id}"}

    if args.discord:
        return format_job_discord(job, args.job_id)

    job["id"] = args.job_id
    return {"ok": True, "job": job}


def cmd_enable(args) -> dict:
    """Enable a job."""
    job = load_job(args.job_id)

    if job is None:
        return {"ok": False, "error": f"Job not found: {args.job_id}"}

    if job.get("enabled", True):
        return {"ok": True, "message": f"Job '{args.job_id}' is already enabled"}

    job["enabled"] = True
    save_job(args.job_id, job)

    return {"ok": True, "message": f"Enabled job '{args.job_id}'"}


def cmd_disable(args) -> dict:
    """Disable a job."""
    job = load_job(args.job_id)

    if job is None:
        return {"ok": False, "error": f"Job not found: {args.job_id}"}

    if not job.get("enabled", True):
        return {"ok": True, "message": f"Job '{args.job_id}' is already disabled"}

    job["enabled"] = False
    save_job(args.job_id, job)

    return {"ok": True, "message": f"Disabled job '{args.job_id}'"}


def cmd_remove(args) -> dict:
    """Remove a job."""
    if not delete_job(args.job_id):
        return {"ok": False, "error": f"Job not found: {args.job_id}"}

    return {"ok": True, "message": f"Removed job '{args.job_id}'"}


def cmd_history(args) -> dict | str:
    """Show run history for a job."""
    limit = args.limit or 10

    if args.job_id:
        runs = get_history(args.job_id, limit=limit)
        if args.discord:
            return format_history_discord(args.job_id, runs)
        return {"ok": True, "job_id": args.job_id, "runs": runs}
    else:
        all_history = get_all_history(limit_per_job=limit)
        if args.discord:
            if not all_history:
                return "**No run history.**"
            lines = []
            for job_id, runs in all_history.items():
                lines.append(format_history_discord(job_id, runs))
            return "\n\n".join(lines)
        return {"ok": True, "history": all_history}


def main():
    parser = argparse.ArgumentParser(
        description="Manage Merlin cron jobs — add, list, enable/disable, remove, and view history.",
        epilog="""
Examples:
  # Add a new job (with dry-run preview)
  uv run cron_manage.py add \\
    --schedule "0 9 * * *" \\
    --prompt "Check for new Python releases" \\
    --channel YOUR_CHANNEL_ID \\
    --description "Daily Python check" \\
    --report-mode silent \\
    --dry-run

  # List all jobs (Discord-formatted)
  uv run cron_manage.py list --discord

  # Get job details
  uv run cron_manage.py get daily-python-check --discord

  # Enable/disable a job
  uv run cron_manage.py disable daily-python-check
  uv run cron_manage.py enable daily-python-check

  # Remove a job
  uv run cron_manage.py remove daily-python-check

  # View run history
  uv run cron_manage.py history daily-python-check --discord

Cron expression cheat sheet:
  * * * * *     Every minute
  0 * * * *     Every hour
  0 9 * * *     Daily at 9:00
  0 9 * * 1     Mondays at 9:00
  0 8 * * 1-5   Weekdays at 8:00
  0 */2 * * *   Every 2 hours

Report modes:
  always  - Always send results to Discord (default)
  silent  - Only report when there's something noteworthy

Output:
  Without --discord: JSON (for programmatic use)
  With --discord:    Pre-formatted text ready to send to Discord
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # add
    p_add = subparsers.add_parser("add", help="Add a new cron job")
    p_add.add_argument("--id", help="Job ID (slug). Auto-generated from description if not provided")
    p_add.add_argument("--schedule", required=True, help="Cron expression (e.g., '0 9 * * *')")
    p_add.add_argument("--prompt", required=True, help="The prompt to send to Claude")
    p_add.add_argument("--channel", required=True, help="Discord channel ID")
    p_add.add_argument("--description", help="Human-readable description")
    p_add.add_argument("--report-mode", choices=["always", "silent"], default="always",
                       help="Report mode: always or silent (default: always)")
    p_add.add_argument("--max-turns", type=int, default=0, help="Max agentic turns (0 = unlimited, default: 0)")
    p_add.add_argument("--dry-run", action="store_true", help="Show what would be created without saving")
    p_add.set_defaults(func=cmd_add)

    # list
    p_list = subparsers.add_parser("list", help="List all cron jobs")
    p_list.add_argument("--discord", action="store_true", help="Output formatted for Discord")
    p_list.set_defaults(func=cmd_list)

    # get
    p_get = subparsers.add_parser("get", help="Get a job's details")
    p_get.add_argument("job_id", help="Job ID")
    p_get.add_argument("--discord", action="store_true", help="Output formatted for Discord")
    p_get.set_defaults(func=cmd_get)

    # enable
    p_enable = subparsers.add_parser("enable", help="Enable a job")
    p_enable.add_argument("job_id", help="Job ID")
    p_enable.set_defaults(func=cmd_enable)

    # disable
    p_disable = subparsers.add_parser("disable", help="Disable a job")
    p_disable.add_argument("job_id", help="Job ID")
    p_disable.set_defaults(func=cmd_disable)

    # remove
    p_remove = subparsers.add_parser("remove", help="Remove a job")
    p_remove.add_argument("job_id", help="Job ID")
    p_remove.set_defaults(func=cmd_remove)

    # history
    p_history = subparsers.add_parser("history", help="Show run history")
    p_history.add_argument("job_id", nargs="?", help="Job ID (optional, shows all if omitted)")
    p_history.add_argument("--limit", type=int, help="Max runs to show (default: 10)")
    p_history.add_argument("--discord", action="store_true", help="Output formatted for Discord")
    p_history.set_defaults(func=cmd_history)

    args = parser.parse_args()
    result = args.func(args)

    # Output result
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, indent=2))

    # Exit with error code if not ok
    if isinstance(result, dict) and not result.get("ok", True):
        sys.exit(1)


if __name__ == "__main__":
    main()
