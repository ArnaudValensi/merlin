"""Filesystem helpers — path validation, directory listing, file reading, type detection."""

import mimetypes
import shutil
import stat
from pathlib import Path
from typing import Any

# Blocked pseudo-filesystem prefixes
BLOCKED_PREFIXES = ("/proc/", "/sys/", "/dev/")

# Image extensions for inline preview
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico"}

# Audio extensions for inline preview
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".webm", ".opus"}

# Video extensions for inline preview
VIDEO_EXTENSIONS = {".mp4", ".webm", ".ogv", ".mov", ".mkv", ".avi"}

# 3D model extensions for inline preview
MODEL_3D_EXTENSIONS = {".stl", ".obj"}

# Text file max size for reading content (2 MB)
TEXT_MAX_BYTES = 2 * 1024 * 1024

# Common text extensions (beyond what mimetypes detects)
TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".log",
    ".env",
    ".env.example",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".html",
    ".htm",
    ".xml",
    ".sql",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".pl",
    ".lua",
    ".r",
    ".R",
    ".swift",
    ".m",
    ".zig",
    ".jai",
    ".odin",
    ".nim",
    ".ex",
    ".exs",
    ".erl",
    ".hs",
    ".ml",
    ".mli",
    ".clj",
    ".cljs",
    ".lisp",
    ".el",
    ".vim",
    ".dockerfile",
    ".makefile",
    ".cmake",
    ".mmd",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    ".prettierrc",
    ".eslintrc",
    ".babelrc",
}

# Files with no extension that are typically text
TEXT_FILENAMES = {
    "Makefile",
    "Dockerfile",
    "Vagrantfile",
    "Gemfile",
    "Rakefile",
    "LICENSE",
    "README",
    "CHANGELOG",
    "AUTHORS",
    "CONTRIBUTING",
    "CLAUDE.md",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
}


def sanitize_filename(name: str) -> str:
    """Sanitize a filename for safe filesystem use.

    Strips path separators, rejects empty/dot-only names and null bytes.
    Returns the safe filename (always a single path component).
    Raises ValueError if the name is invalid.
    """
    # Reject null bytes — can truncate paths in C-level syscalls
    if "\x00" in name:
        raise ValueError("Invalid filename")

    # Take only the final component (strip directory parts)
    name = name.replace("\\", "/")
    name = name.split("/")[-1]
    name = name.strip()

    if not name or name in (".", ".."):
        raise ValueError("Invalid filename")

    return name


def validate_path(path_str: str) -> Path:
    """Validate and resolve a filesystem path.

    Returns the resolved Path.
    Raises ValueError if the path is blocked or invalid.
    """
    if not path_str:
        path_str = "/"

    path = Path(path_str).resolve()

    # Check blocked prefixes
    path_s = str(path)
    if path_s != "/":
        for prefix in BLOCKED_PREFIXES:
            if path_s.startswith(prefix) or path_s + "/" == prefix:
                raise ValueError(f"Access to {prefix.rstrip('/')} is not allowed")

    # Also block exact matches to /proc, /sys, /dev
    if path_s in ("/proc", "/sys", "/dev"):
        raise ValueError(f"Access to {path_s} is not allowed")

    return path


def list_directory(path: Path) -> dict[str, Any]:
    """List directory contents.

    Returns dict with type, path, and entries list.
    Each entry has: name, type (dir/file), size, mtime, is_hidden.
    Raises ValueError if path is not a directory.
    Raises PermissionError if access denied.
    """
    if not path.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    if not path.is_dir():
        raise ValueError(f"Not a directory: {path}")

    entries = []
    try:
        for item in path.iterdir():
            try:
                st = item.stat()
                is_dir = stat.S_ISDIR(st.st_mode)
                entry = {
                    "name": item.name,
                    "type": "dir" if is_dir else "file",
                    "size": st.st_size if not is_dir else None,
                    "mtime": st.st_mtime,
                    "is_hidden": item.name.startswith("."),
                }
                if is_dir:
                    entry["has_git"] = (item / ".git").exists()
                entries.append(entry)
            except (PermissionError, OSError):
                # Include entry but mark as inaccessible
                entries.append(
                    {
                        "name": item.name,
                        "type": "unknown",
                        "size": None,
                        "mtime": None,
                        "is_hidden": item.name.startswith("."),
                    }
                )
    except PermissionError:
        raise PermissionError(f"Permission denied: {path}")

    # Sort: directories first, then alphabetically (case-insensitive)
    entries.sort(
        key=lambda e: (
            0 if e["type"] == "dir" else 1,
            str(e["name"]).lower(),
        )
    )

    return {
        "type": "directory",
        "path": str(path),
        "entries": entries,
    }


def get_file_info(path: Path) -> dict[str, Any]:
    """Get file metadata for display.

    Returns dict with type, path, name, size, mtime, is_text, is_image, mime_type.
    """
    if not path.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    if path.is_dir():
        raise ValueError(f"Path is a directory: {path}")

    st = path.stat()
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    ext = path.suffix.lower()

    is_image = ext in IMAGE_EXTENSIONS
    is_audio = ext in AUDIO_EXTENSIONS
    is_video = ext in VIDEO_EXTENSIONS
    is_3d_model = ext in MODEL_3D_EXTENSIONS
    is_text = _is_text_file(path, mime_type)

    return {
        "type": "file",
        "path": str(path),
        "name": path.name,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "is_text": is_text,
        "is_image": is_image,
        "is_audio": is_audio,
        "is_video": is_video,
        "is_3d_model": is_3d_model,
        "mime_type": mime_type,
    }


def read_text_file(path: Path) -> dict[str, Any]:
    """Read a text file's content up to TEXT_MAX_BYTES.

    Returns dict with content, size, truncated flag, line_count.
    """
    if not path.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    size = path.stat().st_size
    truncated = size > TEXT_MAX_BYTES

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(TEXT_MAX_BYTES)
    except (PermissionError, OSError) as e:
        raise PermissionError(f"Cannot read file: {e}")

    line_count = content.count("\n") + (
        1 if content and not content.endswith("\n") else 0
    )

    return {
        "content": content,
        "size": size,
        "truncated": truncated,
        "line_count": line_count,
    }


def _is_text_file(path: Path, mime_type: str) -> bool:
    """Determine if a file is likely a text file."""
    ext = path.suffix.lower()

    # Known text extensions
    if ext in TEXT_EXTENSIONS:
        return True

    # Known text filenames
    if path.name in TEXT_FILENAMES:
        return True

    # MIME-type based detection
    if mime_type.startswith("text/"):
        return True
    if mime_type in (
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-sh",
        "application/x-yaml",
    ):
        return True

    # No extension — try reading first bytes to detect binary
    if not ext:
        try:
            with open(path, "rb") as f:
                chunk = f.read(8192)
            # If it has null bytes, it's likely binary
            if b"\x00" in chunk:
                return False
            # If it decodes as UTF-8, it's likely text
            try:
                chunk.decode("utf-8")
                return True
            except UnicodeDecodeError:
                return False
        except (PermissionError, OSError):
            return False

    return False


def create_item(directory: Path, name: str, item_type: str) -> Path:
    """Create a new file or directory.

    Uses atomic operations (mkdir / open with 'x' flag) — no TOCTOU race.
    Returns the created Path.
    Raises ValueError for invalid type or name.
    Raises FileExistsError if target already exists.
    """
    safe_name = sanitize_filename(name)
    target = directory / safe_name

    if item_type == "dir":
        try:
            target.mkdir()
        except FileExistsError:
            raise FileExistsError(f"Already exists: {safe_name}")
    elif item_type == "file":
        try:
            # 'x' flag = create exclusively, fails if exists (atomic)
            with open(target, "x"):
                pass
        except FileExistsError:
            raise FileExistsError(f"Already exists: {safe_name}")
    else:
        raise ValueError("type must be 'file' or 'dir'")

    return target


def _check_not_shallow(path: Path) -> None:
    """Block destructive operations on root or top-level directories.

    Prevents catastrophic mistakes like deleting / or /home.
    Paths must have at least 2 components (e.g., /home/user).
    """
    parts = path.resolve().parts  # ('/', 'home', 'user', ...)
    if len(parts) <= 2:
        raise PermissionError(f"Cannot modify system path: {path}")


def rename_item(path: Path, new_name: str) -> Path:
    """Rename a file or directory.

    Returns the new Path.
    Raises ValueError for invalid name.
    Raises FileNotFoundError if source doesn't exist.
    Raises FileExistsError if target already exists.
    Raises PermissionError for root/top-level paths.
    """
    _check_not_shallow(path)

    # sanitize_filename guarantees a single component — destination
    # stays in the same parent directory as source, so the path
    # validation from the route layer covers the destination too.
    safe_name = sanitize_filename(new_name)
    new_path = path.parent / safe_name

    if not path.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    if new_path.exists():
        raise FileExistsError(f"Already exists: {safe_name}")

    path.rename(new_path)
    return new_path


def delete_item(path: Path) -> None:
    """Delete a file or directory.

    Raises FileNotFoundError if path doesn't exist.
    Raises PermissionError for root/top-level paths.
    Symlinks are removed without following the target.
    Directories are removed recursively.
    """
    _check_not_shallow(path)

    if not path.exists() and not path.is_symlink():
        raise FileNotFoundError(f"Path not found: {path}")

    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
