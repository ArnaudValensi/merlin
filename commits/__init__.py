"""Commit browser module — integrates into the Merlin dashboard."""

from .routes import router, COMMITS_STATIC_DIR

__all__ = ["router", "COMMITS_STATIC_DIR"]
