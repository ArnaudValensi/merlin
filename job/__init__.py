"""
Job core module: scheduler loop and runner subprocess.

The scheduler runs job/runner.py at the start of every minute,
replacing the system cron dependency. Started from main.py as a
core feature that always runs. A run's notification is not the
scheduler's: the attention watcher (notifications/) sees the run's
window finish like any other agent window.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from structured_log import log_event

_JOB_DIR = Path(__file__).parent.resolve()

logger = logging.getLogger("merlin.job")


async def _run_job_runner() -> None:
    """Run job/runner.py as a subprocess and log a crash."""
    start = datetime.now()

    proc = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "runner.py",
        cwd=str(_JOB_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await proc.communicate()
    duration = (datetime.now() - start).total_seconds()

    if proc.returncode != 0:
        error_msg = stderr.decode()[:500]

        log_event(
            "job_runner_crash",
            exit_code=proc.returncode,
            duration=duration,
            stderr=error_msg,
        )

        logger.error(
            "Job runner crashed (exit %d, %.1fs): %s",
            proc.returncode,
            duration,
            error_msg[:200],
        )


async def _job_scheduler() -> None:
    """Run job/runner.py at the start of every minute (replaces cron)."""
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        _console = logging.StreamHandler()
        _console.setFormatter(logging.Formatter("[jobs]      %(message)s"))
        logger.addHandler(_console)
        logger.propagate = False

    logger.info("Job scheduler started")

    while True:
        # Sleep until the next minute starts
        now = datetime.now()
        seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(seconds_until_next_minute)

        # Fire and forget - run in background
        asyncio.create_task(_run_job_runner())


async def start() -> None:
    """Start the job scheduler as a background task."""
    asyncio.create_task(_job_scheduler())
