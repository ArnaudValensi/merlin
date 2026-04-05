"""Commit browser — FastAPI routes (pages + API)."""

import asyncio
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .git_parser import (
    _find_repo_root,
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

# Startup CWD — set by main.py, used as fallback when ?repo= is not provided
_startup_cwd: str = "/"
_home_dir: str = str(Path.home())


def set_startup_cwd(cwd: str) -> None:
    """Set the startup CWD (called by main.py)."""
    global _startup_cwd
    _startup_cwd = cwd


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


def _resolve_repo(repo: str) -> Path:
    """Resolve a ?repo= param to a git repository root.

    If repo is empty, uses startup CWD.
    Raises HTTPException 400 if the path is not a git repo.
    """
    search_dir = repo if repo else _startup_cwd
    p = Path(search_dir)
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {search_dir}")
    root = _find_repo_root(str(p))
    if root is None:
        raise HTTPException(status_code=400, detail=f"Not a git repository: {search_dir}")
    return root


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/commits", response_class=HTMLResponse)
def commits_page(request: Request, repo: str = ""):
    return templates.TemplateResponse("commits.html", {
        "request": request,
        "startup_cwd": _startup_cwd,
        "home_dir": _home_dir,
    })


@router.get("/commits/{commit_hash}", response_class=HTMLResponse)
def commit_detail_page(request: Request, commit_hash: str, repo: str = ""):
    _validate_hash(commit_hash)
    return templates.TemplateResponse("commits.html", {
        "request": request,
        "commit_hash": commit_hash,
        "startup_cwd": _startup_cwd,
        "home_dir": _home_dir,
    })


@router.get("/commits/{commit_hash}/file/{file_path:path}", response_class=HTMLResponse)
def commit_file_page(request: Request, commit_hash: str, file_path: str, repo: str = ""):
    _validate_hash(commit_hash)
    _validate_path(file_path)
    return templates.TemplateResponse("commits.html", {
        "request": request,
        "commit_hash": commit_hash,
        "file_path": file_path,
        "startup_cwd": _startup_cwd,
        "home_dir": _home_dir,
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
    repo: str = "",
):
    """Paginated commit list with stats."""
    if limit < 1 or limit > 200:
        limit = 50
    if skip < 0:
        skip = 0

    repo_dir = _resolve_repo(repo)

    try:
        commits = get_commits(repo_dir=repo_dir, skip=skip, limit=limit, search=search, since=since, until=until)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return commits


@router.get("/api/commits/{commit_hash}")
def api_commit_detail(commit_hash: str, repo: str = ""):
    """Single commit metadata with file stats."""
    _validate_hash(commit_hash)
    repo_dir = _resolve_repo(repo)
    try:
        return get_commit_detail(commit_hash, repo_dir=repo_dir)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/commits/{commit_hash}/diff")
def api_commit_diff(commit_hash: str, repo: str = ""):
    """Parsed unified diff for a commit."""
    _validate_hash(commit_hash)
    repo_dir = _resolve_repo(repo)
    try:
        return get_commit_diff(commit_hash, repo_dir=repo_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/commits/{commit_hash}/file/{file_path:path}")
def api_commit_file(commit_hash: str, file_path: str, repo: str = ""):
    """Full file content with gutter annotations."""
    _validate_hash(commit_hash)
    _validate_path(file_path)
    repo_dir = _resolve_repo(repo)
    try:
        return get_file_with_gutters(commit_hash, file_path, repo_dir=repo_dir)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Git Repository Search (T5)
# ---------------------------------------------------------------------------


@router.get("/api/git/repos")
async def api_git_repos(q: str = ""):
    """Find git repositories matching a fuzzy query. Runs fd on every request."""
    import main as _main
    fd_binary = _main.FD_BINARY
    if not fd_binary:
        raise HTTPException(status_code=500, detail="fd is not available")

    home = str(Path.home())
    try:
        proc = await asyncio.create_subprocess_exec(
            fd_binary, "-H", "--no-ignore", r"^\.git$", home,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"fd failed: {e}")

    if proc.returncode != 0 and not stdout:
        raise HTTPException(status_code=500, detail=f"fd error: {stderr.decode().strip()}")

    # Parse: each line is /path/to/repo/.git — strip /.git suffix
    repos = []
    for line in stdout.decode().strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.endswith("/.git"):
            repo_path = line[:-5]
        elif line.endswith("/.git/"):
            repo_path = line[:-6]
        else:
            continue
        repos.append(repo_path)

    # Filter by query if provided
    if q:
        q_lower = q.lower()
        repos = [r for r in repos if q_lower in r.lower()]

    repos.sort()
    return repos
