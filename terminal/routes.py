"""Web terminal — WebSocket PTY bridge with tmux persistence."""

import asyncio
import fcntl
import json
import logging
import os
import pty
import secrets
import signal
import struct
import tempfile
import termios
import time
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse

from auth import require_auth, verify_ws_cookie
from merlin_ext import make_templates

logger = logging.getLogger("merlin.terminal")

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TERMINAL_DIR = Path(__file__).parent.resolve()
TERMINAL_TEMPLATES_DIR = TERMINAL_DIR / "templates"

templates = make_templates(TERMINAL_TEMPLATES_DIR)

router = APIRouter()

TMUX_SESSION = "merlin-dev"

MAX_AUDIO_SIZE = 25 * 1024 * 1024

# Clipboard image upload directory
CLIPBOARD_DIR = Path("/tmp/merlin-clipboard")
CLIPBOARD_MAX_AGE = 3600  # 1 hour

# CWD — set by main.py at startup, determines terminal starting directory
_cwd: str | None = None


def _voice_available() -> bool:
    """Check if any transcription backend is available."""
    if os.getenv("MERLIN_SAAS_TOKEN") or os.getenv("OPENAI_API_KEY"):
        return True
    try:
        import faster_whisper  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# PTY registry — allows transcribe endpoint to write directly to the terminal
# ---------------------------------------------------------------------------

_pty_registry: dict[str, int] = {}


def register_pty(session_key: str, fd: int) -> None:
    """Register a PTY file descriptor for server-side text injection."""
    _pty_registry[session_key] = fd


def unregister_pty(session_key: str) -> None:
    """Unregister a PTY file descriptor."""
    _pty_registry.pop(session_key, None)


def get_pty_fd(session_key: str) -> int | None:
    """Get the PTY file descriptor for a session, or None if not registered."""
    return _pty_registry.get(session_key)


def set_cwd(cwd: str) -> None:
    """Set the terminal starting directory."""
    global _cwd
    _cwd = cwd


# ---------------------------------------------------------------------------
# PTY helpers
# ---------------------------------------------------------------------------


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    """Set the window size of a PTY."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _read_pty(fd: int) -> str | None:
    """Blocking read from PTY fd. Returns decoded string or None on EOF."""
    try:
        data = os.read(fd, 4096)
        if not data:
            return None
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _sync_clipboard(text: str) -> None:
    """Write clipboard text to sync file (atomic) for merlin-clip paste."""
    CLIPBOARD_DIR.mkdir(exist_ok=True)
    tmp = CLIPBOARD_DIR / ".current.tmp"
    target = CLIPBOARD_DIR / "current.txt"
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.rename(target)
    except OSError:
        pass


@router.get("/terminal", response_class=HTMLResponse)
def terminal_page(request: Request, _auth=Depends(require_auth)):
    # Check if tmux is available (set by main.py)
    import main as _main

    if not getattr(_main, "TMUX_AVAILABLE", True):
        return HTMLResponse(
            "<h2>Terminal unavailable</h2>"
            "<p>tmux is not installed. The web terminal requires tmux.</p>"
            f"<p>Install: <code>{_main._install_cmd('tmux')}</code></p>",
            status_code=503,
        )
    return templates.TemplateResponse(
        request, "terminal.html", {"voice_available": _voice_available()}
    )


@router.get("/clipboard-test", response_class=HTMLResponse)
def clipboard_test_page(request: Request, _auth=Depends(require_auth)):
    return templates.TemplateResponse(request, "clipboard-test.html")


_VALID_LANGUAGES = frozenset(
    ("en", "fr", "de", "es", "it", "pt", "nl", "ja", "zh", "ko")
)


def _unlink_safe(path: str) -> None:
    """Delete a file, ignoring errors."""
    try:
        os.unlink(path)
    except OSError:
        pass


async def _transcribe_and_inject(
    tmp_path: str, language: str, pty_fd: int, auto_enter: bool
) -> None:
    """Transcribe audio and write result directly to the PTY."""
    from transcribe import transcribe

    try:
        text = await asyncio.get_event_loop().run_in_executor(
            None, transcribe, tmp_path, language
        )
        if text:
            os.write(pty_fd, text.encode("utf-8"))
            if auto_enter:
                os.write(pty_fd, b"\r")
    except OSError:
        logger.warning("PTY write failed (fd may be closed)")
    except Exception:
        logger.exception("Background transcription failed")
    finally:
        _unlink_safe(tmp_path)


@router.post("/api/transcribe")
async def transcribe_audio(
    file: UploadFile,
    language: str = Form("en"),
    auto_enter: str = Form("false"),
    _auth=Depends(require_auth),
):
    """Transcribe an uploaded audio file.

    If a PTY is registered (WebSocket active), returns 202 and injects
    text server-side. Otherwise falls back to returning the text in the
    response body (200).
    """
    # Validate language
    lang = language.strip().lower()[:5]
    if lang not in _VALID_LANGUAGES:
        lang = "en"

    # Read and validate size
    content = await file.read()
    if len(content) > MAX_AUDIO_SIZE:
        return JSONResponse({"error": "Audio too large (max 25 MB)"}, status_code=413)

    # Save uploaded audio to a temp file (whisper needs a file path)
    suffix = Path(file.filename or "audio.webm").suffix or ".webm"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(content)
    tmp.close()

    pty_fd = get_pty_fd("terminal")

    if pty_fd is not None:
        # Server-side injection: return immediately, transcribe in background
        should_enter = auto_enter.lower() in ("true", "1")
        asyncio.create_task(
            _transcribe_and_inject(tmp.name, lang, pty_fd, should_enter)
        )
        return JSONResponse({"status": "accepted"}, status_code=202)

    # Fallback: synchronous transcription (phone must stay connected)
    from transcribe import transcribe

    try:
        text = await asyncio.get_event_loop().run_in_executor(
            None, transcribe, tmp.name, lang
        )
        return JSONResponse({"text": text})
    except Exception as e:
        logger.exception("Transcription failed")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        _unlink_safe(tmp.name)


def _cleanup_clipboard() -> None:
    """Delete clipboard images older than CLIPBOARD_MAX_AGE."""
    if not CLIPBOARD_DIR.exists():
        return
    now = time.time()
    for f in CLIPBOARD_DIR.iterdir():
        try:
            if now - f.stat().st_mtime > CLIPBOARD_MAX_AGE:
                f.unlink()
        except OSError:
            pass


_IMAGE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


@router.post("/api/upload-image")
async def upload_image(file: UploadFile, _auth=Depends(require_auth)):
    """Save an uploaded image to /tmp/merlin-clipboard/ and return its path."""
    ct = (file.content_type or "").lower()
    if not ct.startswith("image/"):
        return JSONResponse({"error": "Not an image"}, status_code=400)

    content = await file.read()
    if len(content) > 25 * 1024 * 1024:
        return JSONResponse({"error": "Image too large (25MB max)"}, status_code=413)

    _cleanup_clipboard()
    CLIPBOARD_DIR.mkdir(exist_ok=True)

    ext = _IMAGE_EXT.get(ct, Path(file.filename or "image.png").suffix or ".png")
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}{ext}"
    path = CLIPBOARD_DIR / name
    path.write_bytes(content)

    return JSONResponse({"path": str(path)})


@router.get("/api/terminal/cwd")
async def api_terminal_cwd(_auth=Depends(require_auth)):
    """Get the current working directory of the active tmux pane."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "display-message",
            "-p",
            "-t",
            TMUX_SESSION,
            "#{pane_current_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception:
        return JSONResponse({"cwd": None, "is_git_repo": False, "repo_root": None})

    if proc.returncode != 0 or not stdout.strip():
        return JSONResponse({"cwd": None, "is_git_repo": False, "repo_root": None})

    cwd = stdout.decode().strip()

    # Check if it's a git repo
    try:
        git_proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--show-toplevel",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        git_stdout, _ = await git_proc.communicate()
    except Exception:
        return JSONResponse({"cwd": cwd, "is_git_repo": False, "repo_root": None})

    if git_proc.returncode == 0 and git_stdout.strip():
        repo_root = git_stdout.decode().strip()
        return JSONResponse({"cwd": cwd, "is_git_repo": True, "repo_root": repo_root})

    return JSONResponse({"cwd": cwd, "is_git_repo": False, "repo_root": None})


@router.websocket("/ws/terminal")
async def terminal_ws(websocket: WebSocket):
    # Auth: verify session cookie (browsers send cookies on WebSocket upgrade)
    if not verify_ws_cookie(websocket):
        await websocket.close(code=4401, reason="Unauthorized")
        return

    await websocket.accept()
    logger.info("Terminal WebSocket connected")

    # Fork a PTY running tmux
    pid, master_fd = pty.fork()
    logger.info("pty.fork() returned: pid=%d, fd=%d", pid, master_fd)

    if pid == 0:
        # Child process — start in CWD (or project root)
        os.chdir(_cwd or str(PROJECT_ROOT))
        tmux_conf = TERMINAL_DIR / "tmux.conf"
        tmux_args = ["tmux"]
        if tmux_conf.exists():
            tmux_args += ["-f", str(tmux_conf)]
        tmux_args += [
            "new-session",
            "-A",  # attach if exists, create if not
            "-s",
            TMUX_SESSION,
            "-x",
            "120",
            "-y",
            "40",
        ]
        os.execvp("tmux", tmux_args)
        os._exit(1)

    # Parent process — bridge WebSocket <-> PTY
    register_pty("terminal", master_fd)
    loop = asyncio.get_event_loop()

    # Check if child is still alive
    try:
        result = os.waitpid(pid, os.WNOHANG)
        if result[0] != 0:
            logger.error(
                "tmux child exited immediately: pid=%d status=%d", result[0], result[1]
            )
        else:
            logger.info("tmux child running: pid=%d", pid)
    except ChildProcessError:
        logger.error("tmux child already gone (ChildProcessError)")

    async def pty_to_ws():
        """Read from PTY and send to WebSocket."""
        try:
            while True:
                data = await loop.run_in_executor(None, _read_pty, master_fd)
                if data is None:
                    logger.info("pty_to_ws: got EOF from PTY")
                    break
                await websocket.send_text(data)
        except (WebSocketDisconnect, asyncio.CancelledError):
            logger.info("pty_to_ws: WebSocket disconnected or cancelled")
        except Exception:
            logger.exception("pty_to_ws error")

    async def ws_to_pty():
        """Read from WebSocket and write to PTY."""
        try:
            while True:
                msg = await websocket.receive_text()
                # Check for control messages (JSON with "type" field)
                if msg.startswith("{"):
                    try:
                        parsed = json.loads(msg)
                        msg_type = parsed.get("type")
                        if msg_type == "resize":
                            cols = min(max(int(parsed.get("cols", 120)), 1), 500)
                            rows = min(max(int(parsed.get("rows", 40)), 1), 500)
                            _set_winsize(master_fd, cols, rows)
                            continue
                        if msg_type == "clipboard_sync":
                            _sync_clipboard(parsed.get("text", ""))
                            continue
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                        pass
                # Regular input — write to PTY
                os.write(master_fd, msg.encode("utf-8"))
        except (WebSocketDisconnect, asyncio.CancelledError):
            logger.info("ws_to_pty: WebSocket disconnected or cancelled")
        except Exception:
            logger.exception("ws_to_pty error")

    pty_reader = asyncio.create_task(pty_to_ws())
    ws_reader = asyncio.create_task(ws_to_pty())

    try:
        done, pending = await asyncio.wait(
            [pty_reader, ws_reader], return_when=asyncio.FIRST_COMPLETED
        )
        logger.info(
            "WebSocket loop ended: done=%s, pending=%s",
            [t.get_name() for t in done],
            [t.get_name() for t in pending],
        )
        for task in pending:
            task.cancel()
    finally:
        unregister_pty("terminal")
        logger.info("Cleanup: closing fd=%d, killing pid=%d", master_fd, pid)
        # Close PTY fd and kill the tmux client process (NOT the session)
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGHUP)
            os.waitpid(pid, os.WNOHANG)
        except (OSError, ChildProcessError):
            pass
        logger.info("Terminal WebSocket disconnected")
