"""Shared timezone helper for job scheduling.

Both the preview endpoint (routes.py) and the runner interpret job schedules
in the timezone named by ``JOB_TIMEZONE`` (default UTC; the old ``CRON_TIMEZONE``
name is still honored as a deprecated alias). This module is the single source
of truth for resolving that timezone, so the preview shown in the UI matches
when jobs actually fire.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import paths
from dotenv import load_dotenv

load_dotenv(paths.bot_config_path())


def job_timezone_default() -> ZoneInfo:
    """Return the configured job timezone, or UTC if unset/invalid.

    Pure aside from reading ``JOB_TIMEZONE`` (or the deprecated ``CRON_TIMEZONE``
    alias) from the environment.
    """
    # CRON_TIMEZONE is the deprecated alias, still honored for back-compat.
    name = os.getenv("JOB_TIMEZONE") or os.getenv("CRON_TIMEZONE")
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")
