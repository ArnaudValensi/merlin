"""File browser — FastAPI routes (pages + API)."""

import zipfile
from collections.abc import Generator
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from starlette.responses import StreamingResponse
from zipstream import ZipStream

from merlin_ext import make_templates

from .fs_helpers import (
    create_item,
    delete_item,
    get_file_info,
    list_directory,
    read_text_file,
    rename_item,
    sanitize_filename,
    validate_path,
)

FILES_DIR = Path(__file__).parent.resolve()
FILES_TEMPLATES_DIR = FILES_DIR / "templates"
FILES_STATIC_DIR = FILES_DIR / "static"

templates = make_templates(FILES_TEMPLATES_DIR)

# The framework mounts these under the module's slug: api_router at
# /api/files and page_router at /files (both authed). Routes declare paths
# relative to that namespace, no hardcoded module prefix.
api_router = APIRouter()
page_router = APIRouter()

# CWD — set by main.py at startup, determines default browse path
_cwd: str = "/"


def set_cwd(cwd: str) -> None:
    """Set the default browse directory."""
    global _cwd
    _cwd = cwd


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@page_router.get("", response_class=HTMLResponse)
def files_page(request: Request):
    return templates.TemplateResponse(request, "files.html", {"startup_cwd": _cwd})


@page_router.get("/{path:path}", response_class=HTMLResponse)
def files_path_page(request: Request, path: str):
    return templates.TemplateResponse(request, "files.html", {"startup_cwd": _cwd})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _find_git_root(path: Path) -> str | None:
    """Walk up from path to find the nearest .git (directory or file for submodules)."""
    current = path
    while current != current.parent:
        if (current / ".git").exists():
            return str(current)
        current = current.parent
    if (current / ".git").exists():
        return str(current)
    return None


@api_router.get("/browse")
def api_browse(path: str = Query("/", description="Filesystem path to browse")):
    """Browse a path — returns directory listing or file info."""
    try:
        resolved = validate_path(path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    try:
        if resolved.is_dir():
            result = list_directory(resolved)
            result["repo_root"] = _find_git_root(resolved)
            return result
        else:
            return get_file_info(resolved)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_router.get("/content")
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


@api_router.get("/raw")
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


@api_router.post("/upload")
async def api_upload(
    directory: str = Form(...),
    files: list[UploadFile] = File(...),
):
    """Upload one or more files to a directory."""
    try:
        resolved_dir = validate_path(directory)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not resolved_dir.exists():
        raise HTTPException(status_code=404, detail=f"Directory not found: {directory}")

    if not resolved_dir.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    uploaded = []
    for upload in files:
        try:
            safe_name = sanitize_filename(upload.filename or "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        dest = resolved_dir / safe_name
        try:
            content = await upload.read()
            dest.write_bytes(content)
        except PermissionError:
            raise HTTPException(
                status_code=403, detail=f"Permission denied: {safe_name}"
            )

        uploaded.append({"name": safe_name, "size": len(content)})

    return {"uploaded": uploaded}


# ---------------------------------------------------------------------------
# Download (single file or zip archive)
# ---------------------------------------------------------------------------


class DownloadRequest(BaseModel):
    paths: list[str]


def _collect_files(path: Path, base: Path) -> Generator[tuple[Path, str], None, None]:
    """Yield (absolute_path, arcname) for all files under a directory."""
    for child in sorted(path.rglob("*")):
        try:
            resolved = child.resolve()
        except OSError:
            continue
        # Skip blocked paths (symlinks pointing to /proc, /sys, /dev)
        path_s = str(resolved)
        if any(path_s.startswith(p) for p in ("/proc/", "/sys/", "/dev/")):
            continue
        if child.is_file():
            yield resolved, str(child.relative_to(base))


def _build_zip(items: list[tuple[Path, bool]], base: Path) -> ZipStream:
    """Build a streaming zip archive.

    Files are read lazily as the response is consumed, so the whole archive
    never sits in RAM (or on disk) — memory stays flat regardless of size.
    Directories are expanded via _collect_files so the symlink/blocked-path
    filtering is preserved; we add each resolved file individually rather than
    letting ZipStream recurse, so it can't bypass those checks.
    """
    zs = ZipStream(compress_type=zipfile.ZIP_DEFLATED)
    for path, is_dir in items:
        if is_dir:
            for file_path, arcname in _collect_files(path, base):
                zs.add_path(file_path, arcname)
        else:
            zs.add_path(path, str(path.relative_to(base)))
    return zs


@api_router.post("/download")
def api_download(req: DownloadRequest):
    """Download files/directories as a zip archive (or single file directly)."""
    if not req.paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    # Validate all paths
    resolved: list[Path] = []
    for p in req.paths:
        try:
            r = validate_path(p)
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))
        if not r.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {p}")
        resolved.append(r)

    # Single file → direct download (no zip)
    if len(resolved) == 1 and resolved[0].is_file():
        return FileResponse(path=str(resolved[0]), filename=resolved[0].name)

    # Determine zip filename and base path
    if len(resolved) == 1:
        # Single directory
        zip_name = f"{resolved[0].name}.zip"
        base = resolved[0].parent
    else:
        # Multiple items — find common parent
        parents = [p.parent for p in resolved]
        common = parents[0]
        for parent in parents[1:]:
            # Walk up until we find a common ancestor
            while common != parent and common not in parent.parents:
                common = common.parent
        zip_name = f"{common.name}.zip" if common.name else "download.zip"
        base = common

    # Build item list
    items: list[tuple[Path, bool]] = [(p, p.is_dir()) for p in resolved]

    return StreamingResponse(
        _build_zip(items, base),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


# ---------------------------------------------------------------------------
# File management API (create, rename, delete)
# ---------------------------------------------------------------------------


class CreateRequest(BaseModel):
    path: str
    name: str
    type: Literal["file", "dir"]


class RenameRequest(BaseModel):
    path: str
    new_name: str


class DeleteRequest(BaseModel):
    path: str


@api_router.post("/create")
def api_create(req: CreateRequest):
    """Create a new file or directory."""
    try:
        resolved_dir = validate_path(req.path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not resolved_dir.exists():
        raise HTTPException(status_code=404, detail="Directory not found")

    if not resolved_dir.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    try:
        created = create_item(resolved_dir, req.name, req.type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    return {"created": str(created), "name": created.name, "type": req.type}


@api_router.post("/rename")
def api_rename(req: RenameRequest):
    """Rename a file or directory."""
    try:
        resolved = validate_path(req.path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    try:
        new_path = rename_item(resolved, req.new_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    return {"renamed": str(new_path), "new_name": new_path.name}


@api_router.post("/delete")
def api_delete(req: DeleteRequest):
    """Delete a file or directory."""
    try:
        resolved = validate_path(req.path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    try:
        delete_item(resolved)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    return {"deleted": str(resolved)}
