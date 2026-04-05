"""File browser module — integrates into the Merlin dashboard."""

from .routes import router, FILES_STATIC_DIR

__all__ = ["router", "FILES_STATIC_DIR"]
