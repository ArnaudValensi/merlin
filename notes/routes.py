"""Notes editor — FastAPI routes (pages + API)."""

import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .frontmatter import parse_frontmatter

import paths


def _notes_dir() -> Path:
    return paths.notes_dir()


def _media_dir() -> Path:
    return paths.notes_dir() / "media"


NOTES_DIR = Path(__file__).parent.resolve()
NOTES_TEMPLATES_DIR = NOTES_DIR / "templates"
NOTES_STATIC_DIR = NOTES_DIR / "static"

# Shared templates dir (for base.html) + notes templates
templates = Jinja2Templates(directory=[str(NOTES_TEMPLATES_DIR), str(paths.app_dir() / "templates")])

router = APIRouter()

# Non-note files to exclude
EXCLUDE_FILES = {".history.json", "digest-history.json"}
ALLOWED_EXTENSIONS = {".md"}

# Safe path pattern — prevent path traversal
SAFE_PATH_RE = re.compile(r"^[\w\-./]+$")


def _validate_path(path: str) -> Path:
    """Validate and resolve a note path. Returns absolute path to the .md file."""
    if not SAFE_PATH_RE.match(path):
        raise HTTPException(status_code=400, detail="Invalid path")
    if ".." in path:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    # Add .md extension if not present
    if not path.endswith(".md"):
        path = path + ".md"

    mem = _notes_dir()
    full = (mem / path).resolve()

    # Must be inside notes dir
    if not str(full).startswith(str(mem)):
        raise HTTPException(status_code=400, detail="Path outside notes directory")

    return full


def _slugify(name: str) -> str:
    """Slugify a filename for safe storage."""
    name = re.sub(r"[^\w\s\-.]", "", name)
    name = re.sub(r"\s+", "-", name)
    return name


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/notes", response_class=HTMLResponse)
def notes_index(request: Request):
    return templates.TemplateResponse("notes_index.html", {"request": request})


@router.get("/notes/tags/{tag}", response_class=HTMLResponse)
def notes_tag(request: Request, tag: str):
    return templates.TemplateResponse("notes_tag.html", {"request": request, "tag": tag})


@router.get("/notes/{path:path}", response_class=HTMLResponse)
def notes_view(request: Request, path: str):
    # Serve media files directly
    if path.startswith("media/"):
        mem = _notes_dir()
        media_path = (mem / path).resolve()
        if not str(media_path).startswith(str(mem)):
            raise HTTPException(status_code=400, detail="Invalid path")
        if not media_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(media_path)

    file_path = _validate_path(path)
    is_new = request.query_params.get("new") == "1"

    if not is_new and not file_path.exists():
        raise HTTPException(status_code=404, detail="Note not found")

    return templates.TemplateResponse("notes_view.html", {
        "request": request,
        "note_path": path,
        "new_note": is_new,
    })


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.get("/api/notes")
def api_list_notes():
    """List all notes with metadata."""
    mem = _notes_dir()
    notes = []
    for md_file in sorted(mem.rglob("*.md")):
        if md_file.name in EXCLUDE_FILES:
            continue
        # Skip files inside media/
        rel = md_file.relative_to(mem)
        if str(rel).startswith("media"):
            continue

        content = md_file.read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(content)
        stat = md_file.stat()

        # Path without .md extension for clean URLs
        path_str = str(rel.with_suffix(""))

        notes.append({
            "path": path_str,
            "filename": md_file.name,
            "title": meta.get("title", md_file.stem.replace("-", " ").title()),
            "summary": meta.get("summary", ""),
            "tags": meta.get("tags", []),
            "related": meta.get("related", []),
            "created": meta.get("created", ""),
            "mtime": stat.st_mtime,
        })

    # Sort by most recently modified first
    notes.sort(key=lambda n: n["mtime"], reverse=True)
    return notes


def _build_search_index() -> tuple[list[str], dict[str, tuple[str, list[str]]]]:
    """Build a list of searchable lines and a file metadata lookup.

    Returns (fzf_lines, file_meta) where:
    - fzf_lines: ["path:linenum:line_text", ...] for piping into fzf
    - file_meta: {path_str: (title, [lines])} for context lookup
    """
    mem = _notes_dir()
    fzf_lines = []
    file_meta = {}

    for md_file in sorted(mem.rglob("*.md")):
        if md_file.name in EXCLUDE_FILES:
            continue
        rel = md_file.relative_to(mem)
        if str(rel).startswith("media"):
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        meta, _ = parse_frontmatter(content)
        title = meta.get("title", md_file.stem.replace("-", " ").title())
        path_str = str(rel.with_suffix(""))
        lines = content.split("\n")
        file_meta[path_str] = (title, lines)

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped:
                fzf_lines.append(f"{path_str}\t{i + 1}\t{stripped}")

    return fzf_lines, file_meta


@router.get("/api/notes/search")
def api_search_notes(q: str = ""):
    """Full-text content search across all notes (fuzzy via fzf)."""
    query = q.strip()
    if len(query) < 2:
        return {"query": query, "results": [], "total": 0, "truncated": False}

    max_results = 50
    fzf_lines, file_meta = _build_search_index()
    if not fzf_lines:
        return {"query": query, "results": [], "total": 0, "truncated": False}

    # Pipe all lines through fzf --filter for ranked fuzzy matching
    # --nth=3 searches only the line content (not path or linenum)
    # --delimiter=\t splits on tabs
    try:
        proc = subprocess.run(
            ["fzf", "--filter", query, "--delimiter", "\t", "--nth", "3"],
            input="\n".join(fzf_lines),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"query": query, "results": [], "total": 0, "truncated": False}

    results = []
    for line in proc.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue

        path_str, linenum_str, matched_line = parts
        line_number = int(linenum_str)
        title, lines = file_meta.get(path_str, ("", []))

        ctx_before = ""
        ctx_after = ""
        if lines:
            idx = line_number - 1
            ctx_before = next((lines[j].strip() for j in range(idx - 1, -1, -1) if lines[j].strip()), "")
            ctx_after = next((lines[j].strip() for j in range(idx + 1, len(lines)) if lines[j].strip()), "")

        results.append({
            "path": path_str,
            "title": title,
            "line_number": line_number,
            "line": matched_line,
            "context_before": ctx_before,
            "context_after": ctx_after,
        })
        if len(results) >= max_results:
            break

    return {
        "query": query,
        "results": results,
        "total": len(results),
        "truncated": len(results) >= max_results,
    }


@router.get("/api/notes/sync-status")
def api_sync_status():
    """Return git sync status including push state and any conflicted files."""
    from .sync import conflicted_files, sync_state
    return {
        "conflicted_files": list(conflicted_files),
        "has_conflicts": len(conflicted_files) > 0,
        **sync_state,
    }


@router.post("/api/notes/sync-test")
async def api_sync_test(request: Request):
    """Test if a git remote URL is accessible (runs git ls-remote)."""
    body = await request.json()
    remote_url = body.get("remote_url", "").strip()
    if not remote_url:
        raise HTTPException(status_code=400, detail="No remote URL provided")
    from .sync import test_remote
    ok, message = await test_remote(remote_url, _notes_dir())
    return {"ok": ok, "message": message}


@router.get("/api/notes/{path:path}")
def api_read_note(path: str):
    """Read a note's raw markdown content."""
    file_path = _validate_path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Note not found")
    return JSONResponse({"content": file_path.read_text(encoding="utf-8")})


@router.put("/api/notes/{path:path}")
async def api_save_note(request: Request, path: str):
    """Save note content, git commit and push."""
    file_path = _validate_path(path)

    body = await request.json()
    content = body.get("content", "")

    # Write file (git sync watcher handles commit/push)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    return {"status": "saved"}


@router.delete("/api/notes/{path:path}")
async def api_delete_note(path: str):
    """Delete a note, git commit and push."""
    file_path = _validate_path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Note not found")

    # Delete file (git sync watcher handles commit/push)
    file_path.unlink()

    return {"status": "deleted"}


@router.post("/api/notes/upload")
async def api_upload_media(file: UploadFile):
    """Upload a media file to notes/media/."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename")

    media = _media_dir()
    media.mkdir(parents=True, exist_ok=True)

    safe_name = _slugify(file.filename)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    dest = media / safe_name

    # Avoid overwriting — append number if exists
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        i = 1
        while dest.exists():
            dest = media / f"{stem}-{i}{suffix}"
            i += 1

    data = await file.read()
    dest.write_bytes(data)

    # Return relative path from notes dir for use in markdown
    # (git sync watcher handles commit/push)
    rel = dest.relative_to(_notes_dir())
    return {
        "path": str(rel),
        "filename": dest.name,
    }
