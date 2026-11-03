"""
Job management script — create, list, edit, enable/disable, remove jobs.

Used by the jobs skill to ensure reliable, validated operations.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# When run as subprocess (uv run manage.py), add project root and lib/ to sys.path
_PROJECT_ROOT = str(Path(__file__).parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_LIB_DIR = str(Path(_PROJECT_ROOT) / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from croniter import croniter

import paths
from argparse_help import HelpfulParser
from job.agents import BYPASS_WARNING, DEFAULT_AGENT, agent_ids
from job.state import get_all_history, get_history

JOBS_DIR = paths.jobs_dir()

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
    """Convert a cron expression to a human-readable description.

    Delegates to the maintained ``cron-descriptor`` library so every valid
    expression — including ones the Custom schedule field produces — gets a
    correct English description. Falls back to the raw expression on any error
    (e.g. an invalid expression), preserving the original behavior.
    """
    try:
        from cron_descriptor import Options, get_description

        # Force 24-hour times so the description is deterministic regardless of
        # the host locale (a 12-hour locale would otherwise render "09:00 AM").
        options = Options()
        options.use_24hour_time_format = True
        description = get_description(expression, options)
        # Lowercase just the leading character for a clean "every minute" /
        # "at 09:00" reading without lowercasing names like "Monday".
        if description:
            return description[0].lower() + description[1:]
        return expression
    except Exception:
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
    path = JOBS_DIR / f"{job_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


# Fields of earlier versions. A job file carrying them still loads; they
# drop at the next save.
LEGACY_FIELDS = ("max_turns", "ephemeral", "discord_channel")


def save_job(job_id: str, job: dict) -> Path:
    """Save a job to disk, dropping the fields of earlier versions. Returns
    the path."""
    for key in LEGACY_FIELDS:
        job.pop(key, None)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = JOBS_DIR / f"{job_id}.json"
    path.write_text(json.dumps(job, indent=2))
    return path


def list_jobs() -> list[dict]:
    """List all jobs with their IDs."""
    jobs = []
    if not JOBS_DIR.exists():
        return jobs

    for path in sorted(JOBS_DIR.glob("*.json")):
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
    path = JOBS_DIR / f"{job_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_add(args) -> dict:
    """Add a new job."""
    # An explicitly-empty --schedule (e.g. `--schedule "$CRON"` with $CRON
    # unset) is a mistake, not a request for a trigger-less job — reject it so
    # it can't silently create a job that never runs. Omitting --schedule
    # entirely (None) is the intended way to make a webhook- or manual-only job.
    if args.schedule is not None and not args.schedule.strip():
        return {
            "ok": False,
            "error": (
                "--schedule was given but empty. Omit --schedule entirely for "
                "a webhook- or manual-only job."
            ),
        }
    # Validate cron expression when a schedule is given.
    if args.schedule and not validate_cron(args.schedule):
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
    command = getattr(args, "shell_command", None)
    if command is not None and not command.strip():
        return {
            "ok": False,
            "error": "--command was given but empty (an unset shell variable?).",
        }
    job: dict = {
        "description": args.description or job_id,
        "enabled": True,
        "report_mode": args.report_mode,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    if command is not None:
        job.update(type="command", command=command)
    else:
        job.update(agent=getattr(args, "agent", DEFAULT_AGENT), prompt=args.prompt)
    working_dir = getattr(args, "working_dir", None)
    if working_dir:
        resolved = Path(working_dir).expanduser().resolve()
        if not resolved.is_dir():
            return {"ok": False, "error": f"Not a directory: {working_dir}"}
        job["working_dir"] = str(resolved)
    if args.schedule:
        job["schedule"] = args.schedule
    if getattr(args, "webhook", False):
        from job.webhook import generate_secret

        job["webhook"] = {"secret": generate_secret()}

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
    # The warning goes to stderr so stdout stays one JSON document.
    if command is None:
        print(f"Warning: {BYPASS_WARNING}", file=sys.stderr)

    result = {
        "ok": True,
        "job_id": job_id,
        "path": str(path),
        "message": f"Created job '{job_id}'",
    }
    if job.get("webhook"):
        from job.webhook import public_url

        result["webhook_url"] = public_url(job_id)
        result["webhook_secret"] = job["webhook"]["secret"]
    if not job.get("schedule") and not job.get("webhook"):
        result["note"] = (
            "This job has no schedule and no webhook — it runs only when "
            "triggered manually (merlin job trigger)."
        )
    return result


def cmd_list(args) -> dict:
    """List all jobs."""
    jobs = list_jobs()
    return {"ok": True, "jobs": jobs, "count": len(jobs)}


def cmd_get(args) -> dict:
    """Get a single job's details."""
    job = load_job(args.job_id)

    if job is None:
        return {"ok": False, "error": f"Job not found: {args.job_id}"}

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


def cmd_trigger(args) -> dict:
    """Run a job immediately, bypassing the schedule check.

    Runs in-process, blocks until the run finishes, and reports the job's
    real exit code instead of a blanket success. Stdout stays one JSON
    document; the bypass warning, like every warning, goes to stderr.
    """
    from job import runner

    if args.job_id not in runner.load_all_jobs():
        return {"ok": False, "error": f"Job not found: {args.job_id}"}

    try:
        result = runner.run_single_job(args.job_id)
    except SystemExit:
        return {"ok": False, "error": f"Job '{args.job_id}' failed to run"}

    if result is None:
        return {
            "ok": False,
            "error": f"Job '{args.job_id}' is already running (locked)",
        }

    ok = result.exit_code == 0
    response: dict = {
        "ok": ok,
        "job_id": args.job_id,
        "exit_code": result.exit_code,
        "duration_seconds": round(result.duration, 2),
    }
    if result.run_id:
        response["run_id"] = result.run_id
    if result.window:
        response["window"] = result.window
    response["message"] = (
        f"Job '{args.job_id}' completed"
        if ok
        else f"Job '{args.job_id}' failed (exit {result.exit_code})"
    )
    return response


def cmd_url(args) -> dict:
    """Print a job's public webhook URL and secret."""
    from job.webhook import resolve_public_base

    job = load_job(args.job_id)
    if job is None:
        return {"ok": False, "error": f"Job not found: {args.job_id}"}

    secret = (job.get("webhook") or {}).get("secret")
    if not secret:
        return {
            "ok": False,
            "error": (
                f"Job '{args.job_id}' has no webhook trigger. "
                f"Enable it with: merlin job webhook {args.job_id} --enable"
            ),
        }

    base, source = resolve_public_base()
    url = f"{base}/webhooks/job/{args.job_id}"
    result = {
        "ok": True,
        "job_id": args.job_id,
        "url": url,
        "secret": secret,
        "header": "X-Merlin-Webhook-Secret",
        "curl": f"curl -X POST -H 'X-Merlin-Webhook-Secret: {secret}' {url}",
    }
    if source == "ip":
        result["note"] = (
            "This is a detected local address and may not be reachable from "
            "the internet. Set a public URL in Settings (or "
            "MERLIN_DASHBOARD_URL in config.env) if you front Merlin with "
            "your own tunnel or proxy."
        )
    return result


def cmd_webhook(args) -> dict:
    """Manage a job's webhook trigger: show, --enable, --disable, --rotate."""
    from job.webhook import generate_secret, public_url

    job = load_job(args.job_id)
    if job is None:
        return {"ok": False, "error": f"Job not found: {args.job_id}"}

    has_hook = bool((job.get("webhook") or {}).get("secret"))

    if args.enable:
        if has_hook:
            return {
                "ok": True,
                "message": f"Job '{args.job_id}' already has a webhook trigger",
                "url": public_url(args.job_id),
                "secret": job["webhook"]["secret"],
            }
        job["webhook"] = {"secret": generate_secret()}
        save_job(args.job_id, job)
        return {
            "ok": True,
            "message": f"Webhook trigger enabled for '{args.job_id}'",
            "url": public_url(args.job_id),
            "secret": job["webhook"]["secret"],
        }

    if args.disable:
        if not has_hook:
            return {
                "ok": True,
                "message": f"Job '{args.job_id}' has no webhook trigger",
            }
        job.pop("webhook", None)
        save_job(args.job_id, job)
        return {
            "ok": True,
            "message": f"Webhook trigger removed from '{args.job_id}'",
        }

    if args.rotate:
        if not has_hook:
            return {
                "ok": False,
                "error": f"Job '{args.job_id}' has no webhook trigger to rotate",
            }
        job["webhook"]["secret"] = generate_secret()
        save_job(args.job_id, job)
        return {
            "ok": True,
            "message": f"Webhook secret rotated for '{args.job_id}' (the old secret stops working immediately)",
            "url": public_url(args.job_id),
            "secret": job["webhook"]["secret"],
        }

    # No flag: show status
    result: dict = {"ok": True, "job_id": args.job_id, "webhook_enabled": has_hook}
    if has_hook:
        result["url"] = public_url(args.job_id)
        result["secret"] = job["webhook"]["secret"]
    return result


def cmd_test(args) -> dict:
    """Fire a job's webhook against the local server (the real public path).

    Exercises the whole chain — front desk, secret check, single-flight,
    launch — exactly as an external sender would, just from localhost.
    """
    import urllib.error
    import urllib.request

    job = load_job(args.job_id)
    if job is None:
        return {"ok": False, "error": f"Job not found: {args.job_id}"}

    secret = (job.get("webhook") or {}).get("secret")
    if not secret:
        return {
            "ok": False,
            "error": (
                f"Job '{args.job_id}' has no webhook trigger. "
                f"Enable it with: merlin job webhook {args.job_id} --enable"
            ),
        }

    url = f"http://127.0.0.1:{args.port}/webhooks/job/{args.job_id}"
    req = urllib.request.Request(
        url, method="POST", headers={"X-Merlin-Webhook-Secret": secret}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {
            "ok": False,
            "http_status": e.code,
            "error": f"Webhook fire rejected with HTTP {e.code}",
        }
    except (urllib.error.URLError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        return {
            "ok": False,
            "error": (
                f"Could not reach the Merlin server on port {args.port} "
                f"({reason}). Is it running? Use --port if it serves elsewhere."
            ),
        }

    status = data.get("status")
    message = (
        "Webhook fired, run launched"
        if status == "launched"
        else "Webhook accepted, a run is already active (coalesced)"
    )
    return {
        "ok": True,
        "status": status,
        "run_id": data.get("run_id"),
        "message": message,
    }


def cmd_history(args) -> dict:
    """Show run history for a job."""
    limit = args.limit or 10

    if args.job_id:
        runs = get_history(args.job_id, limit=limit)
        return {"ok": True, "job_id": args.job_id, "runs": runs}
    all_history = get_all_history(limit_per_job=limit)
    return {"ok": True, "history": all_history}


def main(argv: list[str] | None = None, prog: str | None = None):
    parser = HelpfulParser(
        prog=prog,
        description="Manage Merlin jobs: add, list, enable/disable, remove, and view history.",
        epilog="""
Examples:
  # Add a new job (with dry-run preview)
  merlin job add \\
    --schedule "0 9 * * *" \\
    --prompt "Check for new Python releases" \\
    --description "Daily Python check" \\
    --report-mode silent \\
    --dry-run

  # List all jobs
  merlin job list

  # Get job details
  merlin job get daily-python-check

  # Enable/disable a job
  merlin job disable daily-python-check
  merlin job enable daily-python-check

  # Remove a job
  merlin job remove daily-python-check

  # Run a job immediately (bypasses the schedule check)
  merlin job trigger daily-python-check

  # Webhook trigger: enable, inspect, fire, rotate
  merlin job webhook daily-python-check --enable
  merlin job url daily-python-check
  merlin job test daily-python-check
  merlin job webhook daily-python-check --rotate

  # View run history
  merlin job history daily-python-check

Cron expression cheat sheet:
  * * * * *     Every minute
  0 * * * *     Every hour
  0 9 * * *     Daily at 9:00
  0 9 * * 1     Mondays at 9:00
  0 8 * * 1-5   Weekdays at 8:00
  0 */2 * * *   Every 2 hours

Report modes (Merlin notifications, from the run's window):
  always  - Notify when a run finishes, fails or needs an answer (default)
  silent  - Notify only when a run fails or needs an answer
  off     - Never notify for the run

Every run opens a window in the merlin-jobs tmux session, where you can
watch it or step in. Agent jobs run with every permission check bypassed:
give a job only work you would let run unattended.

Output:
  JSON, one document, for programmatic use. Warnings go to stderr.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # add
    p_add = subparsers.add_parser("add", help="Add a new job")
    p_add.add_argument(
        "--id", help="Job ID (slug). Auto-generated from description if not provided"
    )
    p_add.add_argument(
        "--schedule",
        help="Cron expression (e.g., '0 9 * * *'). Omit for a webhook- or manual-only job",
    )
    what = p_add.add_mutually_exclusive_group(required=True)
    what.add_argument("--prompt", help="The prompt the agent runs")
    what.add_argument(
        "--command",
        dest="shell_command",  # "command" is the subcommand's own dest
        help="The shell command to run, instead of an agent",
    )
    p_add.add_argument(
        "--working-dir",
        help="Where the run starts (default: where Merlin was launched, else $HOME)",
    )
    p_add.add_argument(
        "--agent",
        choices=agent_ids(),
        default=DEFAULT_AGENT,
        help=f"The agent that runs the prompt (default: {DEFAULT_AGENT})",
    )
    p_add.add_argument("--description", help="Human-readable description")
    p_add.add_argument(
        "--report-mode",
        choices=["always", "silent", "off"],
        default="always",
        help="Notify: always, silent (failures and questions only), or off (default: always)",
    )
    p_add.add_argument(
        "--webhook",
        action="store_true",
        help="Also enable the webhook trigger (generates a secret)",
    )
    p_add.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without saving",
    )
    p_add.set_defaults(func=cmd_add)

    # list
    p_list = subparsers.add_parser("list", help="List all jobs")
    p_list.set_defaults(func=cmd_list)

    # get
    p_get = subparsers.add_parser("get", help="Get a job's details")
    p_get.add_argument("job_id", help="Job ID")
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

    # trigger
    p_trigger = subparsers.add_parser(
        "trigger", help="Run a job immediately (bypasses schedule check)"
    )
    p_trigger.add_argument("job_id", help="Job ID")
    p_trigger.set_defaults(func=cmd_trigger)

    # url
    p_url = subparsers.add_parser(
        "url", help="Print a job's public webhook URL and secret"
    )
    p_url.add_argument("job_id", help="Job ID")
    p_url.set_defaults(func=cmd_url)

    # webhook
    p_webhook = subparsers.add_parser(
        "webhook", help="Manage a job's webhook trigger (show/enable/disable/rotate)"
    )
    p_webhook.add_argument("job_id", help="Job ID")
    hook_action = p_webhook.add_mutually_exclusive_group()
    hook_action.add_argument(
        "--enable",
        action="store_true",
        help="Enable the webhook trigger (generates a secret)",
    )
    hook_action.add_argument(
        "--disable", action="store_true", help="Remove the webhook trigger"
    )
    hook_action.add_argument(
        "--rotate",
        action="store_true",
        help="Replace the secret (the old one stops working immediately)",
    )
    p_webhook.set_defaults(func=cmd_webhook)

    # test
    p_test = subparsers.add_parser(
        "test", help="Fire a job's webhook against the local server"
    )
    p_test.add_argument("job_id", help="Job ID")
    p_test.add_argument(
        "--port",
        type=int,
        default=3123,
        help="Local Merlin server port (default: 3123)",
    )
    p_test.set_defaults(func=cmd_test)

    # history
    p_history = subparsers.add_parser("history", help="Show run history")
    p_history.add_argument(
        "job_id", nargs="?", help="Job ID (optional, shows all if omitted)"
    )
    p_history.add_argument("--limit", type=int, help="Max runs to show (default: 10)")
    p_history.set_defaults(func=cmd_history)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return
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
