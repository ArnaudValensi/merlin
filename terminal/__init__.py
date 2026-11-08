"""Web terminal module — integrates into the Merlin dashboard."""

from pathlib import Path

from .routes import api_router, page_router, register_routes

# The native terminal's page assets, served at /static/terminal.
STATIC_DIR = Path(__file__).parent / "static"

__all__ = ["STATIC_DIR", "api_router", "page_router", "register_routes"]
