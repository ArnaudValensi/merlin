"""Notes editor module — integrates into the Merlin dashboard."""

from .routes import router, NOTES_STATIC_DIR

__all__ = ["router", "NOTES_STATIC_DIR"]
