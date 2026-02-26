"""Commit browser — FastAPI routes (pages + API)."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .git_parser import (
    get_commits,
    get_commit_detail,
    get_commit_diff,
    get_file_with_gutters,
)

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

COMMITS_DIR = Path(__file__).parent.resolve()
COMMITS_TEMPLATES_DIR = COMMITS_DIR / "templates"
COMMITS_STATIC_DIR = COMMITS_DIR / "static"

# Shared templates dir (for base.html) + commits templates
templates = Jinja2Templates(directory=[str(COMMITS_TEMPLATES_DIR), str(PROJECT_ROOT / "templates")])

router = APIRouter()

# Safe hash pattern
HASH_RE = re.compile(r"^[0-9a-f]{4,40}$")
# Safe file path pattern
SAFE_PATH_RE = re.compile(r"^[\w\-./]+$")


def _validate_hash(h: str) -> str:
    """Validate commit hash parameter."""
    if not HASH_RE.match(h):
        raise HTTPException(status_code=400, detail="Invalid commit hash")
    return h


def _validate_path(path: str) -> str:
    """Validate file path parameter."""
    if ".." in path:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    if path.startswith("/"):
        raise HTTPException(status_code=400, detail="Absolute paths not allowed")
    if not SAFE_PATH_RE.match(path):
        raise HTTPException(status_code=400, detail="Invalid file path")
    return path


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/commits", response_class=HTMLResponse)
def commits_page(request: Request):
    return templates.TemplateResponse("commits.html", {"request": request})


@router.get("/commits/{commit_hash}", response_class=HTMLResponse)
def commit_detail_page(request: Request, commit_hash: str):
    _validate_hash(commit_hash)
    return templates.TemplateResponse("commits.html", {
        "request": request,
        "commit_hash": commit_hash,
    })


@router.get("/commits/{commit_hash}/file/{file_path:path}", response_class=HTMLResponse)
def commit_file_page(request: Request, commit_hash: str, file_path: str):
    _validate_hash(commit_hash)
    _validate_path(file_path)
    return templates.TemplateResponse("commits.html", {
        "request": request,
        "commit_hash": commit_hash,
        "file_path": file_path,
    })


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.get("/api/commits")
def api_list_commits(
    skip: int = 0,
    limit: int = 50,
    search: str = "",
    since: str = "",
    until: str = "",
):
    """Paginated commit list with stats."""
    if limit < 1 or limit > 200:
        limit = 50
    if skip < 0:
        skip = 0

    try:
        commits = get_commits(skip=skip, limit=limit, search=search, since=since, until=until)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return commits


@router.get("/api/commits/{commit_hash}")
def api_commit_detail(commit_hash: str):
    """Single commit metadata with file stats."""
    _validate_hash(commit_hash)
    try:
        return get_commit_detail(commit_hash)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/commits/{commit_hash}/diff")
def api_commit_diff(commit_hash: str):
    """Parsed unified diff for a commit."""
    _validate_hash(commit_hash)
    try:
        return get_commit_diff(commit_hash)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/commits/{commit_hash}/file/{file_path:path}")
def api_commit_file(commit_hash: str, file_path: str):
    """Full file content with gutter annotations."""
    _validate_hash(commit_hash)
    _validate_path(file_path)
    try:
        return get_file_with_gutters(commit_hash, file_path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
