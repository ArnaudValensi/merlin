"""
Extension logging helper — provides correctly namespaced loggers for extensions.

All extension loggers live under the ``merlin.ext.*`` tree and automatically
inherit the RotatingFileHandler configured on the root ``merlin`` logger.

Usage in an extension's main module or any submodule::

    from merlin_ext import get_logger

    logger = get_logger("my-extension")              # → merlin.ext.my_extension
    logger = get_logger("my-extension.submodule")     # → merlin.ext.my_extension.submodule
"""

import logging


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
