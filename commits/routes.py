"""Commit browser — FastAPI routes (pages + API)."""

import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from merlin_ext import make_templates

from . import reviews as rv
from .compare import (
    RefError,
    compare_detail,
    compare_diff,
    compare_file,
    get_commit_detail,
    get_commit_diff,
    get_file_with_gutters,
    list_refs,
    resolve_comparison,
)
from .git_parser import _find_repo_root, get_commits

COMMITS_DIR = Path(__file__).parent.resolve()
COMMITS_TEMPLATES_DIR = COMMITS_DIR / "templates"
COMMITS_STATIC_DIR = COMMITS_DIR / "static"

templates = make_templates(COMMITS_TEMPLATES_DIR)

# api_router → /api/commits, page_router → /commits (both authed by the
# framework). Paths are declared relative to that namespace.
api_router = APIRouter()
page_router = APIRouter()

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


def _validate_review_id(review_id: str) -> str:
    try:
        return rv.validate_id(review_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
        raise HTTPException(
            status_code=400, detail=f"Not a git repository: {search_dir}"
        )
    return root


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@page_router.get("", response_class=HTMLResponse)
def commits_page(request: Request, repo: str = ""):
    return templates.TemplateResponse(
        request,
        "commits.html",
        {
            "startup_cwd": _startup_cwd,
            "home_dir": _home_dir,
        },
    )


# Registered before /{commit_hash} so "compare" is a literal segment, not a
# commit hash (it would fail hash validation with a 400).
@page_router.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request, repo: str = ""):
    return templates.TemplateResponse(
        request,
        "commits.html",
        {"startup_cwd": _startup_cwd, "home_dir": _home_dir},
    )


@page_router.get("/compare/file/{file_path:path}", response_class=HTMLResponse)
def compare_file_page(request: Request, file_path: str, repo: str = ""):
    _validate_path(file_path)
    return templates.TemplateResponse(
        request,
        "commits.html",
        {"file_path": file_path, "startup_cwd": _startup_cwd, "home_dir": _home_dir},
    )


@page_router.get("/reviews", response_class=HTMLResponse)
def reviews_page(request: Request, repo: str = ""):
    return templates.TemplateResponse(
        request,
        "commits.html",
        {"startup_cwd": _startup_cwd, "home_dir": _home_dir},
    )


@page_router.get("/reviews/{review_id}", response_class=HTMLResponse)
def review_page(request: Request, review_id: str, repo: str = ""):
    _validate_review_id(review_id)
    return templates.TemplateResponse(
        request,
        "commits.html",
        {"startup_cwd": _startup_cwd, "home_dir": _home_dir},
    )


@page_router.get(
    "/reviews/{review_id}/file/{file_path:path}", response_class=HTMLResponse
)
def review_file_page(request: Request, review_id: str, file_path: str, repo: str = ""):
    _validate_review_id(review_id)
    _validate_path(file_path)
    return templates.TemplateResponse(
        request,
        "commits.html",
        {"file_path": file_path, "startup_cwd": _startup_cwd, "home_dir": _home_dir},
    )


@page_router.get("/{commit_hash}", response_class=HTMLResponse)
def commit_detail_page(request: Request, commit_hash: str, repo: str = ""):
    _validate_hash(commit_hash)
    return templates.TemplateResponse(
        request,
        "commits.html",
        {
            "commit_hash": commit_hash,
            "startup_cwd": _startup_cwd,
            "home_dir": _home_dir,
        },
    )


@page_router.get("/{commit_hash}/file/{file_path:path}", response_class=HTMLResponse)
def commit_file_page(
    request: Request, commit_hash: str, file_path: str, repo: str = ""
):
    _validate_hash(commit_hash)
    _validate_path(file_path)
    return templates.TemplateResponse(
        request,
        "commits.html",
        {
            "commit_hash": commit_hash,
            "file_path": file_path,
            "startup_cwd": _startup_cwd,
            "home_dir": _home_dir,
        },
    )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@api_router.get("")
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
        commits = get_commits(
            repo_dir=repo_dir,
            skip=skip,
            limit=limit,
            search=search,
            since=since,
            until=until,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return commits


# Registered before /{commit_hash} so "repos" is matched as a literal segment
# and not captured as a commit hash.
@api_router.get("/repos")
async def api_git_repos(q: str = ""):
    """Find git repositories matching a fuzzy query. Runs fd on every request."""
    import main as _main

    fd_binary = _main.FD_BINARY
    if not fd_binary:
        raise HTTPException(status_code=500, detail="fd is not available")

    home = str(Path.home())
    try:
        proc = await asyncio.create_subprocess_exec(
            fd_binary,
            "-H",
            "--no-ignore",
            r"^\.git$",
            home,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"fd failed: {e}")

    if proc.returncode != 0 and not stdout:
        raise HTTPException(
            status_code=500, detail=f"fd error: {stderr.decode().strip()}"
        )

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


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes")


def _comparison(repo: str, base: str, head: str, mergebase: str, worktree: str):
    """Resolve the query of a compare route. Bad refs are a 400."""
    repo_dir = _resolve_repo(repo)
    try:
        cmp = resolve_comparison(
            repo_dir,
            base=base,
            head=head,
            mergebase=_flag(mergebase),
            worktree=_flag(worktree),
        )
    except RefError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return cmp, repo_dir


# The compare and refs routes are registered before /{commit_hash} so their
# first segment is matched as a literal and not captured as a commit hash.
@api_router.get("/compare")
def api_compare(
    repo: str = "",
    base: str = "",
    head: str = "",
    mergebase: str = "",
    worktree: str = "",
):
    """A comparison: kind, resolved refs, included commits, files with stats."""
    cmp, repo_dir = _comparison(repo, base, head, mergebase, worktree)
    try:
        return compare_detail(cmp, repo_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/compare/diff")
def api_compare_diff(
    repo: str = "",
    base: str = "",
    head: str = "",
    mergebase: str = "",
    worktree: str = "",
):
    """Parsed unified diff of a comparison."""
    cmp, repo_dir = _comparison(repo, base, head, mergebase, worktree)
    try:
        return compare_diff(cmp, repo_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/compare/file/{file_path:path}")
def api_compare_file(
    file_path: str,
    repo: str = "",
    base: str = "",
    head: str = "",
    mergebase: str = "",
    worktree: str = "",
):
    """Full file at the head of a comparison, with gutter annotations."""
    _validate_path(file_path)
    cmp, repo_dir = _comparison(repo, base, head, mergebase, worktree)
    try:
        return compare_file(cmp, file_path, repo_dir)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/refs")
def api_refs(repo: str = ""):
    """Local and remote branches, the current branch, the guessed base."""
    repo_dir = _resolve_repo(repo)
    try:
        return list_refs(repo_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Reviews: a saved comparison (one JSON file under ~/.merlin/reviews/)
# ---------------------------------------------------------------------------


class ReviewCreate(BaseModel):
    repo: str = ""
    base: str = ""
    head: str = ""
    mergebase: bool = False
    worktree: bool = False
    title: str | None = None


class ReviewPatch(BaseModel):
    title: str | None = None
    status: str | None = None


class ViewedBody(BaseModel):
    viewed: bool = True


def _load_review(review_id: str) -> dict:
    _validate_review_id(review_id)
    try:
        return rv.load(review_id)
    except rv.ReviewNotFound:
        raise HTTPException(status_code=404, detail=f"Unknown review: {review_id}")
    except rv.ReviewCorrupt as e:
        raise HTTPException(status_code=500, detail=str(e))


def _review_repo(review: dict) -> Path | None:
    """The review's repository root, exactly the stored one, or None when it
    is gone. A stored path that is now a plain directory inside some other
    repository does not count: that would bind the review to the wrong
    repository."""
    repo = review.get("repo") or ""
    p = Path(repo)
    if not repo or not p.is_dir():
        return None
    root = _find_repo_root(str(p))
    if root is None or root.resolve() != p.resolve():
        return None
    return root


def _review_comparison(review_id: str):
    """The review's comparison, resolved in its own repository. The review
    id is the only input: no query parameter can point it elsewhere."""
    review = _load_review(review_id)
    repo_dir = _review_repo(review)
    if repo_dir is None:
        raise HTTPException(
            status_code=400, detail=f"Repository not found: {review.get('repo')}"
        )
    try:
        cmp = rv.comparison_of(review, repo_dir)
    except RefError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return review, repo_dir, cmp


@api_router.get("/reviews")
def api_reviews_list(repo: str = ""):
    """The repository's reviews, open and closed, newest update first."""
    repo_dir = _resolve_repo(repo)
    return rv.list_for_repo(str(repo_dir.resolve()))


@api_router.post("/reviews", status_code=201)
def api_reviews_create(body: ReviewCreate):
    """Save a comparison as a review. Bad refs are a 400."""
    repo_dir = _resolve_repo(body.repo)
    try:
        cmp = resolve_comparison(
            repo_dir,
            base=body.base,
            head=body.head,
            mergebase=body.mergebase,
            worktree=body.worktree,
        )
    except RefError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        return rv.create(repo_dir, cmp, body.title)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/reviews/{review_id}")
def api_review_get(review_id: str, since: str | None = None):
    """A review with its live comparison.

    A full load (no ``since``) recomputes the viewed state, counts the
    commits since the last look and advances ``last_seen_head``. With
    ``since``, the page's known ``updated`` stamp, this is the poll: it
    answers ``{"changed": false}`` when nothing moved, else the record,
    without touching the review.
    """
    review = _load_review(review_id)
    if since is not None:
        # A "+00:00" offset arrives as a space when the client forgot to
        # encode it, and a stamp never contains a space.
        if review.get("updated") == since.replace(" ", "+"):
            return {"changed": False}
        return {"changed": True, "review": review}

    repo_dir = _review_repo(review)
    if repo_dir is None:
        return {
            "review": review,
            "comparison": None,
            "changed_since_viewed": [],
            "new_commits": 0,
            "error": f"Repository not found: {review.get('repo')}",
        }
    try:
        review, changed, new_commits, cmp = rv.refresh(review_id, repo_dir)
    except RefError as e:
        return {
            "review": review,
            "comparison": None,
            "changed_since_viewed": [],
            "new_commits": 0,
            "error": str(e),
        }
    except rv.ReviewCorrupt as e:
        raise HTTPException(status_code=500, detail=str(e))
    try:
        comparison = compare_detail(cmp, repo_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "review": review,
        "comparison": comparison,
        "changed_since_viewed": changed,
        "new_commits": new_commits,
        "error": None,
    }


@api_router.get("/reviews/{review_id}/diff")
def api_review_diff(review_id: str):
    """Parsed unified diff of the review's comparison, in its repository."""
    _review, repo_dir, cmp = _review_comparison(review_id)
    try:
        return compare_diff(cmp, repo_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/reviews/{review_id}/file/{file_path:path}")
def api_review_file(review_id: str, file_path: str):
    """Full file at the head of the review's comparison, in its repository."""
    _validate_path(file_path)
    _review, repo_dir, cmp = _review_comparison(review_id)
    try:
        return compare_file(cmp, file_path, repo_dir)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_router.patch("/reviews/{review_id}")
def api_review_patch(review_id: str, body: ReviewPatch):
    """Rename a review or open and close it."""
    review = _load_review(review_id)
    try:
        if body.title is not None:
            review = rv.set_title(review_id, body.title)
        if body.status is not None:
            review = rv.set_status(review_id, body.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except rv.ReviewNotFound:
        raise HTTPException(status_code=404, detail=f"Unknown review: {review_id}")
    return review


@api_router.put("/reviews/{review_id}/viewed/{file_path:path}")
def api_review_viewed(review_id: str, file_path: str, body: ViewedBody):
    """Tick or untick a file of the review."""
    _validate_path(file_path)
    review = _load_review(review_id)
    repo_dir = _review_repo(review)
    if repo_dir is None:
        raise HTTPException(
            status_code=400, detail=f"Repository not found: {review.get('repo')}"
        )
    try:
        return rv.set_viewed(review_id, file_path, body.viewed, repo_dir)
    except RefError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except rv.ReviewNotFound:
        raise HTTPException(status_code=404, detail=f"Unknown review: {review_id}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_router.get("/{commit_hash}")
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


@api_router.get("/{commit_hash}/diff")
def api_commit_diff(commit_hash: str, repo: str = ""):
    """Parsed unified diff for a commit."""
    _validate_hash(commit_hash)
    repo_dir = _resolve_repo(repo)
    try:
        return get_commit_diff(commit_hash, repo_dir=repo_dir)
    except RefError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/{commit_hash}/file/{file_path:path}")
def api_commit_file(commit_hash: str, file_path: str, repo: str = ""):
    """Full file content with gutter annotations."""
    _validate_hash(commit_hash)
    _validate_path(file_path)
    repo_dir = _resolve_repo(repo)
    try:
        return get_file_with_gutters(commit_hash, file_path, repo_dir=repo_dir)
    except (FileNotFoundError, RefError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
