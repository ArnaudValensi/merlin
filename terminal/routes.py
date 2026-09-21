"""Web terminal — WebSocket PTY bridge with tmux persistence."""

import asyncio
import codecs
import contextlib
import fcntl
import json
import logging
import os
import pty
import re
import secrets
import struct
import tempfile
import termios
import time
from pathlib import Path

from fastapi import (
    APIRouter,
    Form,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse

from auth import verify_ws_cookie
from board import sweep as board_sweep
from merlin_ext import make_templates
from terminal.pty_bridge import PtyBridge, terminate_client
from terminal.tmux import (
    DEFAULT_SESSION_NAME,
    parse_session_identity,
    reconnect_argv,
    terminal_process_env,
    with_tmux_conf,
)

logger = logging.getLogger("merlin.terminal")

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TERMINAL_DIR = Path(__file__).parent.resolve()
TERMINAL_TEMPLATES_DIR = TERMINAL_DIR / "templates"

templates = make_templates(TERMINAL_TEMPLATES_DIR)

# api_router → /api/terminal, page_router → /terminal (both authed by the
# framework). The /ws/terminal WebSocket is public and self-authenticating
# via the session cookie, so it can't use the HTTP auth dependency — it is
# wired through register_routes(app), the escape hatch, instead.
api_router = APIRouter()
page_router = APIRouter()

MAX_AUDIO_SIZE = 25 * 1024 * 1024
SESSION_REPORT_INTERVAL = 0.5

# Clipboard image upload directory
CLIPBOARD_DIR = Path("/tmp/merlin-clipboard")
CLIPBOARD_MAX_AGE = 3600  # 1 hour

# CWD — set by main.py at startup, determines terminal starting directory
_cwd: str | None = None


async def _read_current_session(tty: str) -> board_sweep.ClientSession | None:
    """Read the exact tmux session currently displayed by one browser client."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, board_sweep.client_session_info, tty)


async def _send_session_frame(
    websocket: WebSocket, current: board_sweep.ClientSession
) -> bool:
    """Send one browser-only session identity control frame."""
    try:
        await websocket.send_text(
            "\x00"
            + json.dumps(
                {
                    "type": "session",
                    "name": current.name,
                    "id": current.session_id,
                    "created": current.created,
                    "window_id": current.window_id,
                    "window_index": current.window_index,
                    "window": current.window_name,
                }
            )
        )
        return True
    except Exception:
        return False


class SessionReportState:
    """Deduplicate session frames shared by panel switches and the watcher."""

    def __init__(self) -> None:
        self.last_reported: board_sweep.ClientSession | None = None
        self.lock = asyncio.Lock()
        # Whether the page is on screen and focused, as it reports through the
        # socket. A backgrounded app keeps its socket open for a while, so the
        # socket alone says nothing about someone looking. True until told
        # otherwise.
        self.visible = True
        self.focused = True
        # A short name for the browser, from its user agent, so a routing
        # record can say which page was looking.
        self.agent = ""


# One entry per connected terminal socket: what its tmux client displays, as
# last reported to the browser, and whether the page is visible and focused.
# Push suppression reads it. Registered on connect, dropped on disconnect.
_client_views: set[SessionReportState] = set()


def _target_of(state: SessionReportState) -> str:
    current = state.last_reported
    return (
        f"{current.name}:{current.window_id}" if current and current.window_id else ""
    )


def looking_at(target: str) -> bool:
    """Whether a connected page is looking at this window right now: visible,
    focused and displaying it. The one thing that silences a notification for
    that window, on every channel: the person is reading it. A hidden page,
    an unfocused one (another workspace, another window in front) or one on
    another tmux window is not looking."""
    for state in list(_client_views):
        if state.visible and state.focused and _target_of(state) == target:
            return True
    return False


def looking_clients() -> list[dict]:
    """One record per page that is visible and focused: the browser and the
    window it displays. What the routing record and the status route show,
    so a silent event can be explained."""
    out: list[dict] = []
    for state in list(_client_views):
        if state.visible and state.focused:
            out.append({"agent": state.agent, "window": _target_of(state)})
    return out


def short_agent(user_agent: str) -> str:
    """``Brave``, ``Safari``, ``Chrome``, ``Firefox`` with the OS when it is
    obvious, from a user agent string. Best effort, for humans."""
    ua = user_agent or ""
    os_name = ""
    for needle, name in (
        ("iPhone", "iPhone"),
        ("iPad", "iPad"),
        ("Android", "Android"),
        ("Mac OS X", "Mac"),
        ("Windows", "Windows"),
        ("Linux", "Linux"),
    ):
        if needle in ua:
            os_name = name
            break
    browser = ""
    for needle, name in (
        ("Brave", "Brave"),
        ("Edg/", "Edge"),
        ("Firefox/", "Firefox"),
        ("CriOS/", "Chrome"),
        ("Chrome/", "Chrome"),
        ("Safari/", "Safari"),
    ):
        if needle in ua:
            browser = name
            break
    return " · ".join(p for p in (os_name, browser) if p)


def displayed_targets() -> set[str]:
    """``session:window_id`` for every window a visible connected client
    displays. Informational."""
    out: set[str] = set()
    for state in list(_client_views):
        current = state.last_reported
        if not state.visible or current is None or not current.window_id:
            continue
        out.add(f"{current.name}:{current.window_id}")
    return out


async def _send_session_if_changed(
    websocket: WebSocket,
    current: board_sweep.ClientSession,
    state: SessionReportState,
) -> bool:
    async with state.lock:
        if current == state.last_reported:
            return False
        if await _send_session_frame(websocket, current):
            state.last_reported = current
            return True
    return False


async def _report_current_session(
    websocket: WebSocket, tty: str, state: SessionReportState | None = None
) -> board_sweep.ClientSession | None:
    """Read and report the current session, returning it after a successful send."""
    report_state = state or SessionReportState()
    current = await _read_current_session(tty)
    if current is not None and await _send_session_if_changed(
        websocket, current, report_state
    ):
        return current
    return None


async def _watch_current_session(
    websocket: WebSocket,
    tty: str,
    state: SessionReportState | None = None,
    *,
    interval: float = SESSION_REPORT_INTERVAL,
) -> None:
    """Report initial attach and later tmux-native per-client switches."""
    report_state = state or SessionReportState()
    while True:
        current = await _read_current_session(tty)
        if current is not None:
            await _send_session_if_changed(websocket, current, report_state)
        await asyncio.sleep(interval)


async def _switch_session(
    websocket: WebSocket,
    tty: str,
    target: str,
    state: SessionReportState | None = None,
) -> None:
    """Per-client switch to ``target`` (``session`` or ``session:window``), then
    report the client's new current session back to the browser."""
    if not target:
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, board_sweep.switch_client, tty, target)
    await _report_current_session(websocket, tty, state)


def _voice_available() -> bool:
    """Check if any transcription backend is available."""
    if os.getenv("MERLIN_SAAS_TOKEN") or os.getenv("OPENAI_API_KEY"):
        return True
    try:
        import faster_whisper  # noqa: F401

        return True
    except ImportError:
        return False


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


@page_router.get("", response_class=HTMLResponse)
def terminal_page(request: Request):
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
        request,
        "terminal.html",
        {
            "voice_available": _voice_available(),
            "home_dir": os.path.expanduser("~"),
        },
    )


@page_router.get("/clipboard-test", response_class=HTMLResponse)
def clipboard_test_page(request: Request):
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


# A well-formed injection target is ``session:window_id``. Session names never
# contain a space or a colon (see board.sweep.sanitize_session_name) and a tmux
# window id is ``@`` plus digits. Anything else takes the 200 fallback.
_TARGET_RE = re.compile(r"^[^:\s]+:@\d+$")


async def _transcribe_and_inject(
    tmp_path: str,
    language: str,
    target: str,
    auto_enter: bool,
) -> None:
    """Transcribe audio and inject the text into the target tmux window.

    ``target`` is ``session:window_id`` (the window the page showed when the
    recording stopped). The text is sent to that window's active pane through
    tmux ``send-keys``, independent of any client, WebSocket or current window:
    a user who switched away sees nothing move and the text waits in the window
    they left. A window that no longer exists is a logged loss, not a redirect
    to somewhere else. Runs off the event loop: every call shells out to tmux.
    """
    from transcribe import transcribe

    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(None, transcribe, tmp_path, language)
        if text:
            # If the target window's pane is scrolled it is in tmux copy-mode, a
            # modal keyboard grab, and the injected text would fire copy-mode key
            # bindings (some kill the pane) instead of landing at the prompt.
            # Cancel the mode on that window first.
            await loop.run_in_executor(None, board_sweep.exit_copy_mode, target)
            if not await loop.run_in_executor(
                None, board_sweep.send_literal, target, text
            ):
                logger.warning(
                    "Transcription injection failed: target=%s text_len=%d",
                    target,
                    len(text),
                )
            elif auto_enter:
                # Send Enter as a separate keystroke, after a brief gap.
                # Concatenating text + Enter in one send makes a TUI treat the
                # Enter as a newline inside the pasted blob instead of a submit.
                # Letting the text flush first makes the standalone Enter submit.
                await asyncio.sleep(0.15)
                await loop.run_in_executor(None, board_sweep.send_enter, target)
    except Exception:
        logger.exception("Background transcription failed")
    finally:
        _unlink_safe(tmp_path)


@api_router.post("/transcribe")
async def transcribe_audio(
    file: UploadFile,
    language: str = Form("en"),
    auto_enter: str = Form("false"),
    target: str = Form(""),
):
    """Transcribe an uploaded audio file.

    With a valid ``target`` (``session:window_id``, captured by the page when
    the recording stopped), returns 202 and injects the text into that tmux
    window in the background. Without a target, or with a malformed one, falls
    back to returning the text in the response body (200) for the page to write
    into its own WebSocket, which already reaches the right device.
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

    target = target.strip()
    if _TARGET_RE.match(target):
        # Server-side injection: return immediately, transcribe in background
        # and send the text straight to the target window.
        should_enter = auto_enter.lower() in ("true", "1")
        asyncio.create_task(
            _transcribe_and_inject(tmp.name, lang, target, should_enter)
        )
        return JSONResponse({"status": "accepted"}, status_code=202)

    # Fallback: synchronous transcription (page writes into its own socket)
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


UPLOAD_MAX_SIZE = 100 * 1024 * 1024  # 100 MB


def _safe_basename(name: str | None) -> str:
    """Return a shell-safe basename: strip path, replace risky chars, cap length."""
    base = Path(name or "").name
    cleaned = []
    for ch in base:
        if ch.isalnum() or ch in "._-":
            cleaned.append(ch)
        else:
            cleaned.append("_")
    out = "".join(cleaned).lstrip(".")
    if len(out) > 80:
        stem = Path(out).stem[: 80 - len(Path(out).suffix)]
        out = stem + Path(out).suffix
    return out or "file"


@api_router.post("/upload-file")
async def upload_file(file: UploadFile):
    """Save an uploaded file to /tmp/merlin-clipboard/ and return its path."""
    content = await file.read()
    if len(content) > UPLOAD_MAX_SIZE:
        return JSONResponse({"error": "File too large (100MB max)"}, status_code=413)

    _cleanup_clipboard()
    CLIPBOARD_DIR.mkdir(exist_ok=True)

    safe = _safe_basename(file.filename)
    name = f"{secrets.token_hex(3)}-{safe}"
    path = CLIPBOARD_DIR / name
    path.write_bytes(content)

    return JSONResponse({"path": str(path)})


@api_router.get("/cwd")
async def api_terminal_cwd():
    """Get the current working directory of the active tmux pane."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "display-message",
            "-p",
            "-t",
            DEFAULT_SESSION_NAME,
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


async def terminal_ws(websocket: WebSocket):
    # Auth: verify session cookie (browsers send cookies on WebSocket upgrade)
    if not verify_ws_cookie(websocket):
        await websocket.close(code=4401, reason="Unauthorized")
        return

    await websocket.accept()
    logger.info("Terminal WebSocket connected")

    preferred = parse_session_identity(
        websocket.query_params.get("session_id"),
        websocket.query_params.get("session_created"),
    )
    sessions = board_sweep.run_session_sweep_checked()
    if sessions is None:
        await websocket.close(code=1013, reason="tmux temporarily unavailable")
        return
    tmux_args = with_tmux_conf(reconnect_argv(sessions, preferred))
    tmux_env = terminal_process_env(os.environ, term="xterm-256color")

    # Fork a PTY running tmux. Nothing but exec-prep may run in the child:
    # its stdio IS the pty, so e.g. logging would leak into the terminal
    # stream (and write to merlin.log from a second process).
    pid, master_fd = pty.fork()

    if pid == 0:
        # Child process — start in CWD (or project root)
        os.chdir(_cwd or str(PROJECT_ROOT))
        os.execvpe("tmux", tmux_args, tmux_env)
        os._exit(1)

    # Parent process — bridge WebSocket <-> PTY. All PTY I/O goes through
    # the event loop (see terminal/pty_bridge.py for why no thread may
    # ever block inside a PTY syscall).
    logger.info("pty.fork() returned: pid=%d, fd=%d", pid, master_fd)

    # The tmux client's tty, derived from the master fd we already hold rather
    # than looked up afterwards. This is exactly tmux's ``client_tty``, so it is
    # what lets a switch target *this* client only and one browser tab never
    # moves another. Deriving it keeps a single code path on Linux and macOS
    # (only the string shape differs) and makes it a per-connection constant:
    # no lookup can fail or name the wrong client, and callers never have an
    # "unknown tty" state to guard against. (tmux still has its own attach
    # window before it registers the client; a command aimed at the tty simply
    # finds nothing until then, which the session watcher rides out by
    # reporting nothing.) It must never degrade to a terminal that runs but
    # cannot switch, so a failure closes the socket.
    try:
        client_tty = os.ptsname(master_fd)
    except OSError:
        logger.exception("Could not resolve the tmux client tty")
        with contextlib.suppress(OSError):
            os.close(master_fd)
        await terminate_client(pid)
        await websocket.close(code=1011, reason="PTY setup failed")
        return

    try:
        bridge = PtyBridge(master_fd)
    except OSError:
        logger.exception("PTY bridge setup failed")
        with contextlib.suppress(OSError):
            os.close(master_fd)
        await terminate_client(pid)
        await websocket.close(code=1011, reason="PTY setup failed")
        return
    session_report_state = SessionReportState()
    session_report_state.agent = short_agent(websocket.headers.get("user-agent", ""))
    _client_views.add(session_report_state)

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
        # Incremental decoder: UTF-8 sequences can split across reads.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                data = await bridge.read()
                if not data:
                    logger.info("pty_to_ws: got EOF from PTY")
                    break
                text = decoder.decode(data)
                if text:
                    await websocket.send_text(text)
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
                            _set_winsize(bridge.fd, cols, rows)
                            continue
                        if msg_type == "clipboard_sync":
                            _sync_clipboard(parsed.get("text", ""))
                            continue
                        if msg_type == "switch":
                            await _switch_session(
                                websocket,
                                client_tty,
                                str(parsed.get("target", "")),
                                session_report_state,
                            )
                            continue
                        if msg_type == "visibility":
                            session_report_state.visible = bool(
                                parsed.get("visible", True)
                            )
                            session_report_state.focused = bool(
                                parsed.get("focused", True)
                            )
                            continue
                        if msg_type == "ping":
                            # Liveness probe from a page coming back to the
                            # foreground: a half-open socket after a phone
                            # suspension sends fine but never answers.
                            await websocket.send_text(
                                "\x00" + json.dumps({"type": "pong"})
                            )
                            continue
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                        pass
                # Regular input — write to PTY
                if not await bridge.write(msg.encode("utf-8")):
                    logger.warning("ws_to_pty: PTY gone or stalled, closing")
                    break
        except (WebSocketDisconnect, asyncio.CancelledError):
            logger.info("ws_to_pty: WebSocket disconnected or cancelled")
        except Exception:
            logger.exception("ws_to_pty error")

    pty_reader = asyncio.create_task(pty_to_ws())
    ws_reader = asyncio.create_task(ws_to_pty())
    session_watcher = asyncio.create_task(
        _watch_current_session(websocket, client_tty, session_report_state)
    )

    try:
        done, pending = await asyncio.wait(
            [pty_reader, ws_reader], return_when=asyncio.FIRST_COMPLETED
        )
        logger.info(
            "WebSocket loop ended: done=%s, pending=%s",
            [t.get_name() for t in done],
            [t.get_name() for t in pending],
        )
    finally:
        # Sync teardown first, so hooks and fds are gone even if this
        # handler is itself cancelled (server shutdown).
        _client_views.discard(session_report_state)
        bridge.close()
        for task in (pty_reader, ws_reader, session_watcher):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(
                pty_reader, ws_reader, session_watcher, return_exceptions=True
            )
        logger.info("Cleanup: PTY closed fd=%d, terminating pid=%d", master_fd, pid)
        # Terminate the tmux client process (NOT the session) and reap it,
        # escalating to SIGKILL if it lingers.
        await terminate_client(pid)
        logger.info("Terminal WebSocket disconnected")


def register_routes(app) -> None:
    """Escape hatch: wire the /ws/terminal WebSocket directly onto the app.

    The framework auto-auths api_router/page_router via an HTTP dependency,
    but a WebSocket upgrade can't use that. terminal owns this path and its
    auth: terminal_ws() self-authenticates against the session cookie
    (browsers send cookies on the WS upgrade) before accepting the socket.
    """
    app.add_api_websocket_route("/ws/terminal", terminal_ws)
