"""Commit browser module — integrates into the Merlin dashboard."""

from .routes import api_router, page_router, COMMITS_STATIC_DIR as STATIC_DIR

__all__ = ["api_router", "page_router", "STATIC_DIR"]
