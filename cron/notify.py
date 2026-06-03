"""Cron notification — delivers engine results to Discord.

The engine has no notion of Discord. It returns text output.
This module decides whether and how to deliver that output.

report_mode controls notification behavior:
  - "always" (default): always send the result to Discord
  - "silent": only send if the job failed (non-zero exit code)
  - "off": never send anything
"""

from __future__ import annotations

import logging

logger = logging.getLogger("merlin.cron")


def notify_cron_result(
    job_id: str,
    job: dict,
    result: dict,
    extension_registry: dict,
) -> None:
    """Send notification about cron execution. Never raises."""
    try:
        _do_notify(job_id, job, result, extension_registry)
    except Exception:
        logger.exception("Notification failed for job %s", job_id)


def _do_notify(
    job_id: str,
    job: dict,
    result: dict,
    extension_registry: dict,
) -> None:
    """Internal: attempt Discord notification if merlin-bot is loaded."""
    # report_mode controls whether to notify
    report_mode = job.get("report_mode", "always")
    exit_code = result.get("exit_code", -1)

    if report_mode == "off":
        # Notifications disabled for this job
        logger.debug("Job %s has notifications off, skipping", job_id)
        return

    if report_mode == "silent" and exit_code == 0:
        # Silent mode: only notify on errors
        logger.debug("Job %s succeeded in silent mode, skipping notification", job_id)
        return

    bot_info = extension_registry.get("merlin-bot")
    if not (bot_info and bot_info.notify is not None and bot_info.module is not None):
        return

    # Channel: per-job discord_channel > job's legacy "channel" field > bot's global default
    channel = job.get("discord_channel") or job.get("channel")
    if channel == "default":
        channel = None
    if not channel:
        channel = _get_bot_default_channel(bot_info.module)
    if not channel:
        logger.debug("No Discord channel configured for job %s", job_id)
        return

    try:
        message = _format_report(job_id, job, result)
        session_id = result.get("session_id")
        bot_info.notify(channel, message, session_id=session_id)
    except Exception:
        logger.exception("Discord notification failed for job %s", job_id)


def _get_bot_default_channel(bot_module: object) -> str | None:
    """Get the first channel from the bot's DISCORD_CHANNEL_IDS set."""
    channels: set[str] = getattr(bot_module, "DISCORD_CHANNEL_IDS", set())
    if channels:
        return next(iter(channels))
    return None


def _format_report(job_id: str, job: dict, result: dict) -> str:
    """Format a cron result into a Discord-friendly message."""
    exit_code = result.get("exit_code", -1)
    status = "\u2705" if exit_code == 0 else "\u274c"
    duration = result.get("duration_seconds", 0)
    cost = result.get("cost_usd")
    desc = job.get("description", job_id)

    lines = [f"{status} **Cron: {desc}** ({job_id})"]
    cost_part = f" | Cost: ${cost:.4f}" if cost else ""
    lines.append(f"Duration: {duration:.1f}s{cost_part}")

    output = result.get("output", "")
    if output:
        lines.append(f"\n{output}")

    return "\n".join(lines)
