"""
Centralized path resolution for Merlin.

Two modes:
  - Dev mode: Running from git checkout. App code in repo root.
  - Installed mode: Running from ~/.merlin/current/.

User data (memory, cron-jobs, logs, config) always lives under ~/.merlin/
regardless of mode. Only app code location differs.

Dev mode detected by (in order):
  1. Explicit override via set_dev_mode()
  2. MERLIN_DEV env var (1/true/yes enables, 0/false/no disables)
  3. .git/ directory present next to this file

Override the base install directory with MERLIN_HOME env var (default: ~/.merlin).
"""

from __future__ import annotations

import os
from pathlib import Path

# Directory containing this file — always the app code root
_THIS_DIR = Path(__file__).parent.resolve()

# Explicit override for dev mode (set by CLI --dev flag)
_dev_mode_override: bool | None = None


def set_dev_mode(enabled: bool) -> None:
    """Explicitly set dev/installed mode. Called by CLI for --dev flag."""
    global _dev_mode_override
    _dev_mode_override = enabled


def is_dev_mode() -> bool:
    """True if running from a git checkout (dev mode)."""
    if _dev_mode_override is not None:
        return _dev_mode_override
    env_val = os.environ.get("MERLIN_DEV", "").lower()
    if env_val in ("1", "true", "yes"):
        return True
    if env_val in ("0", "false", "no"):
        return False
    return (_THIS_DIR / ".git").is_dir()


def merlin_home() -> Path:
    """Base directory for installed Merlin. Default: ~/.merlin."""
    custom = os.environ.get("MERLIN_HOME")
    if custom:
        return Path(custom).resolve()
    return (Path.home() / ".merlin").resolve()


def app_dir() -> Path:
    """Where app code lives.

    Dev:       the repo root (directory containing this file)
    Installed: ~/.merlin/current/
    """
    if is_dev_mode():
        return _THIS_DIR
    return merlin_home() / "current"


def data_dir() -> Path:
    """Where user data lives (memory, cron-jobs, data, logs).

    Always ~/.merlin/ regardless of mode. User data is never in the code repo.
    """
    return merlin_home()


def config_path() -> Path:
    """Main config file path. Always ~/.merlin/config.env."""
    return merlin_home() / "config.env"


def bot_config_path() -> Path:
    """Bot-specific config (Discord token, etc.). Same as config_path()."""
    return merlin_home() / "config.env"


def memory_dir() -> Path:
    """Memory system directory (user.md, kb/, logs/)."""
    return data_dir() / "memory"


def cron_jobs_dir() -> Path:
    """Cron job definitions directory."""
    return data_dir() / "cron-jobs"


def logs_dir() -> Path:
    """Base log directory."""
    return data_dir() / "logs"
