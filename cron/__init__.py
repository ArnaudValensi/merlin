"""
Cron core module — scheduler loop and runner subprocess.

The scheduler runs cron/runner.py at the start of every minute,
replacing the system cron dependency. Started from main.py as a
core feature that always runs.

After the runner completes, the scheduler parses structured JSON
lines from stdout and sends Discord notifications via notify_cron_result().
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from structured_log import log_event

_CRON_DIR = Path(__file__).parent.resolve()

logger = logging.getLogger("merlin.cron")


def _process_runner_output(stdout_bytes: bytes) -> None:
    """Parse runner stdout for job_complete events and send notifications."""
    try:
        from main import extension_registry
    except ImportError:
        return

    from cron.notify import notify_cron_result

    stdout = stdout_bytes.decode(errors="replace")
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        if data.get("type") == "job_complete":
            try:
                notify_cron_result(
                    job_id=data["job_id"],
                    job=data["job"],
                    result=data["result"],
                    extension_registry=extension_registry,
                )
            except Exception:
                logger.warning(
                    "Failed to notify for job %s", data.get("job_id"), exc_info=True
                )


async def _run_cron_runner() -> None:
    """Run cron/runner.py as a subprocess, log crashes, and send notifications."""
    start = datetime.now()

    proc = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "runner.py",
        cwd=str(_CRON_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await proc.communicate()
    duration = (datetime.now() - start).total_seconds()

    if proc.returncode != 0:
        error_msg = stderr.decode()[:500]

        log_event(
            "cron_runner_crash",
            exit_code=proc.returncode,
            duration=duration,
            stderr=error_msg,
        )

        logger.error(
            "Cron runner crashed (exit %d, %.1fs): %s",
            proc.returncode,
            duration,
            error_msg[:200],
        )

        # Alert via Discord if bot is loaded (restores old behavior)
        try:
            from main import extension_registry

            bot_info = extension_registry.get("merlin-bot")
            if bot_info and bot_info.notify is not None and bot_info.module is not None:
                from cron.notify import _get_bot_default_channel

                channel = _get_bot_default_channel(bot_info.module)
                if channel:
                    bot_info.notify(
                        channel,
                        f"**Cron runner crashed** (exit {proc.returncode})\n```\n{error_msg}\n```",
                    )
        except Exception:
            logger.debug("Could not send crash alert to Discord", exc_info=True)

    # Process job results and send Discord notifications (runs in main process
    # where extension_registry is available)
    if stdout:
        try:
            _process_runner_output(stdout)
        except Exception:
            logger.warning(
                "Failed to process runner output for notifications", exc_info=True
            )


async def _cron_scheduler() -> None:
    """Run cron/runner.py at the start of every minute (replaces cron)."""
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        _console = logging.StreamHandler()
        _console.setFormatter(logging.Formatter("[cron]      %(message)s"))
        logger.addHandler(_console)
        logger.propagate = False

    logger.info("Cron scheduler started")

    while True:
        # Sleep until the next minute starts
        now = datetime.now()
        seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(seconds_until_next_minute)

        # Fire and forget - run in background
        asyncio.create_task(_run_cron_runner())


async def start() -> None:
    """Start the cron scheduler as a background task."""
    asyncio.create_task(_cron_scheduler())
