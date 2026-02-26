"""File browser — FastAPI routes (pages + API)."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .fs_helpers import (
    get_file_info,
    list_directory,
    read_text_file,
    validate_path,
)

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

FILES_DIR = Path(__file__).parent.resolve()
FILES_TEMPLATES_DIR = FILES_DIR / "templates"
FILES_STATIC_DIR = FILES_DIR / "static"

# Shared templates dir (for base.html) + files templates
templates = Jinja2Templates(directory=[str(FILES_TEMPLATES_DIR), str(PROJECT_ROOT / "templates")])

router = APIRouter()

# CWD — set by main.py at startup, determines default browse path
_cwd: str = "/"


def set_cwd(cwd: str) -> None:
    """Set the default browse directory."""
    global _cwd
    _cwd = cwd


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/files", response_class=HTMLResponse)
def files_page(request: Request):
    # Redirect to CWD so the file browser starts there
    if _cwd != "/":
        return RedirectResponse(url=f"/files{_cwd}", status_code=302)
    return templates.TemplateResponse("files.html", {"request": request})


@router.get("/files/{path:path}", response_class=HTMLResponse)
def files_path_page(request: Request, path: str):
    return templates.TemplateResponse("files.html", {"request": request})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.get("/api/files/browse")
def api_browse(path: str = Query("/", description="Filesystem path to browse")):
    """Browse a path — returns directory listing or file info."""
    try:
        resolved = validate_path(path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    try:
        if resolved.is_dir():
            return list_directory(resolved)
        else:
            return get_file_info(resolved)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/files/content")
def api_content(path: str = Query(..., description="Filesystem path to read")):
    """Read text file content (up to 2MB)."""
    try:
        resolved = validate_path(path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")

    if resolved.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory")

    try:
        return read_text_file(resolved)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/api/files/raw")
def api_raw(path: str = Query(..., description="Filesystem path to serve")):
    """Serve a raw file (for images, downloads)."""
    try:
        resolved = validate_path(path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")

    if resolved.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory")

    try:
        return FileResponse(
            path=str(resolved),
            filename=resolved.name,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
