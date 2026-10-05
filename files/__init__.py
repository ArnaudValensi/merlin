"""File browser module — integrates into the Merlin dashboard."""

from .html_view import register_routes
from .routes import api_router, page_router, FILES_STATIC_DIR as STATIC_DIR

__all__ = ["api_router", "page_router", "STATIC_DIR", "register_routes"]
