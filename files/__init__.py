"""File browser module — integrates into the Merlin dashboard."""

from .routes import api_router, page_router, FILES_STATIC_DIR as STATIC_DIR

__all__ = ["api_router", "page_router", "STATIC_DIR"]
