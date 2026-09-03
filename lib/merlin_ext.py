"""
Extension helpers — loggers and template factories.

All extension loggers live under the ``merlin.ext.*`` tree and automatically
inherit the RotatingFileHandler configured on the root ``merlin`` logger.

The ``make_templates`` factory creates a :class:`Jinja2Templates` pre-wired
with app-wide globals (``nav_items``, ``saas_mode``, etc.) so templates like
``base.html`` render correctly from any module or extension.

Usage::

    from merlin_ext import get_logger, make_templates

    logger = get_logger("my-extension")
    templates = make_templates(EXT_DIR / "templates")
"""

import logging
import os
import socket
from collections.abc import Callable, Mapping
from pathlib import Path

from fastapi.templating import Jinja2Templates


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``merlin.ext.*`` namespace.

    Dashes in *name* are converted to underscores so logger names are valid
    Python identifiers (consistent with how Python resolves module names).

    Args:
        name: Extension name, optionally with dot-separated sub-components.
              e.g. ``"video-scenes"`` or ``"video-scenes.renderer"``.

    Returns:
        A :class:`logging.Logger` that inherits handlers from ``merlin``.
    """
    safe = name.replace("-", "_")
    return logging.getLogger(f"merlin.ext.{safe}")


def resolve_machine_name(
    environ: Mapping[str, str] | None = None,
    hostname: Callable[[], str] | None = None,
) -> str:
    """Return the stable, user-facing machine label for browser titles.

    Managed containers inherit their environment slug, which is more useful
    than Podman's generated hostname. Self-hosted instances use the OS hostname.
    A failed hostname lookup must not prevent template rendering.
    """
    env = os.environ if environ is None else environ
    environment_slug = env.get("MERLIN_ENVIRONMENT_SLUG", "").strip()
    if environment_slug:
        return environment_slug

    try:
        return ((hostname or socket.gethostname)() or "").strip()
    except OSError:
        return ""


_TEMPLATE_GLOBALS: dict = {"machine_name": resolve_machine_name()}
_INSTANCES: list[Jinja2Templates] = []


def register_template_globals(**kwargs) -> None:
    """Register app-wide values as Jinja2 ``env.globals`` for all templates.

    Updates both future instances (created via :func:`make_templates`) and
    instances already created before this call — so registration order
    doesn't matter.
    """
    _TEMPLATE_GLOBALS.update(kwargs)
    for t in _INSTANCES:
        t.env.globals.update(kwargs)


def make_templates(directory: str | Path | list[str | Path]) -> Jinja2Templates:
    """Create a :class:`Jinja2Templates` with app-wide globals registered.

    The project root ``templates/`` directory is appended as a fallback so
    shared templates (``base.html``, etc.) are always reachable.
    """
    import paths

    if isinstance(directory, (str, Path)):
        dirs = [str(directory)]
    else:
        dirs = [str(d) for d in directory]

    root = str(paths.app_dir() / "templates")
    if root not in dirs:
        dirs.append(root)

    t = Jinja2Templates(directory=dirs)
    t.env.globals.update(_TEMPLATE_GLOBALS)
    _INSTANCES.append(t)
    return t
