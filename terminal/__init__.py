"""Web terminal module — integrates into the Merlin dashboard."""

from .routes import api_router, page_router, register_routes

__all__ = ["api_router", "page_router", "register_routes"]
