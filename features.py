"""
Feature flags for experimental features.

A flagged feature ships in every release but does not exist for anyone who
has not opted in: no nav entry, no page, no CLI namespace, no skill. Opt in
by listing the feature in ``MERLIN_FEATURES`` (comma- or space-separated,
case-insensitive), either in the environment or in ``config.env``:

    MERLIN_FEATURES=app

The environment wins when set, so a test or a one-off run can flip a flag
without touching the config. Changing a flag takes effect on the next start
(``merlin restart``).
"""

from __future__ import annotations

import os
import re

import paths

ENV_VAR = "MERLIN_FEATURES"


def _raw_value() -> str:
    if ENV_VAR in os.environ:
        return os.environ[ENV_VAR]
    config = paths.config_path()
    if not config.is_file():
        return ""
    try:
        from dotenv import dotenv_values

        return dotenv_values(config).get(ENV_VAR) or ""
    except OSError:
        return ""


def active() -> set[str]:
    """Every feature name turned on, lowercased."""
    return {name.lower() for name in re.split(r"[,\s]+", _raw_value()) if name}


def enabled(name: str) -> bool:
    """Whether the feature ``name`` is turned on."""
    return name.lower() in active()
