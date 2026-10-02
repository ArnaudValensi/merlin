"""Apps page, REST API, and the WebRTC signaling WebSocket."""

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse

from merlin_ext import make_templates

APP_DIR = Path(__file__).parent.resolve()
templates = make_templates(APP_DIR / "templates")

api_router = APIRouter()
page_router = APIRouter()


@page_router.get("", response_class=HTMLResponse)
def apps_page(request: Request):
    return templates.TemplateResponse(request, "apps.html", {})


def register_routes(app: FastAPI) -> None:
    """The signaling WebSocket (added in the streaming phase)."""


async def start() -> None:
    """Sweep sessions whose app died while Merlin was down."""
