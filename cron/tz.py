"""Shared timezone helper for cron scheduling.

Both the preview endpoint (routes.py) and the runner interpret cron schedules
in the timezone named by ``CRON_TIMEZONE`` (default UTC). This module is the
single source of truth for resolving that timezone, so the preview shown in the
UI matches when jobs actually fire.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import paths
from dotenv import load_dotenv

load_dotenv(paths.bot_config_path())


def cron_timezone() -> ZoneInfo:
    """Return the configured cron timezone, or UTC if unset/invalid.

    Pure aside from reading ``CRON_TIMEZONE`` from the environment.
    """
    name = os.getenv("CRON_TIMEZONE")
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")
