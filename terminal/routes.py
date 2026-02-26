"""Web terminal — WebSocket PTY bridge with tmux persistence."""

import asyncio
import fcntl
import json
import logging
import os
import pty
import signal
import struct
import tempfile
import termios
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from auth import require_auth, verify_ws_cookie

logger = logging.getLogger("merlin.terminal")

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TERMINAL_DIR = Path(__file__).parent.resolve()
TERMINAL_TEMPLATES_DIR = TERMINAL_DIR / "templates"

templates = Jinja2Templates(
    directory=[str(TERMINAL_TEMPLATES_DIR), str(PROJECT_ROOT / "templates")]
)

router = APIRouter()

TMUX_SESSION = "merlin-dev"

# CWD — set by main.py at startup, determines terminal starting directory
_cwd: str | None = None


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
    return templates.TemplateResponse("terminal.html", {"request": request})


@router.post("/api/transcribe")
async def transcribe_audio(file: UploadFile, language: str = Form("en"), _auth=Depends(require_auth)):
    """Transcribe an uploaded audio file and return the text."""
    from transcribe import transcribe

    # Validate language
    lang = language.strip().lower()[:5]
    if lang not in ("en", "fr", "de", "es", "it", "pt", "nl", "ja", "zh", "ko"):
        lang = "en"

    # Save uploaded audio to a temp file (whisper needs a file path)
    suffix = Path(file.filename or "audio.webm").suffix or ".webm"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            content = await file.read()
            tmp.write(content)

        text = await asyncio.get_event_loop().run_in_executor(
            None, transcribe, tmp_path, lang
        )
        return JSONResponse({"text": text})
    except Exception as e:
        logger.exception("Transcription failed")
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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

    if pid == 0:
        # Child process — start in CWD (or project root)
        os.chdir(_cwd or str(PROJECT_ROOT))
        os.execvp(
            "tmux",
            [
                "tmux",
                "new-session",
                "-A",  # attach if exists, create if not
                "-s",
                TMUX_SESSION,
                "-x",
                "120",
                "-y",
                "40",
            ],
        )
        os._exit(1)

    # Parent process — bridge WebSocket <-> PTY
    loop = asyncio.get_event_loop()

    async def pty_to_ws():
        """Read from PTY and send to WebSocket."""
        try:
            while True:
                data = await loop.run_in_executor(None, _read_pty, master_fd)
                if data is None:
                    break
                await websocket.send_text(data)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
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
                        if parsed.get("type") == "resize":
                            cols = min(max(int(parsed.get("cols", 120)), 1), 500)
                            rows = min(max(int(parsed.get("rows", 40)), 1), 500)
                            _set_winsize(master_fd, cols, rows)
                            continue
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                        pass
                # Regular input — write to PTY
                os.write(master_fd, msg.encode("utf-8"))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            logger.exception("ws_to_pty error")

    pty_reader = asyncio.create_task(pty_to_ws())
    ws_reader = asyncio.create_task(ws_to_pty())

    try:
        done, pending = await asyncio.wait(
            [pty_reader, ws_reader], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    finally:
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
