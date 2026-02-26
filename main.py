# /// script
# dependencies = ["fastapi", "uvicorn[standard]", "jinja2", "python-dotenv", "python-multipart", "discord.py", "httpx", "faster-whisper"]
# ///
"""
Merlin — Portable mobile dev environment.

Launch on any Linux machine to get a web-based development environment
accessible from anywhere via Cloudflare tunnel.

Core modules: File browser, Terminal, Commit browser, Notes editor.
Apps: Optional plugins (e.g., merlin-bot) that add pages to the sidebar.

Usage:
    uv run main.py                    # Start on port 3123, CWD = current dir
    uv run main.py --port 8080        # Custom port
    uv run main.py --no-tunnel        # Local access only (no Cloudflare tunnel)
    uv run main.py --host 127.0.0.1   # Bind to localhost only
"""

import argparse
import asyncio
import logging
import os
import secrets
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

import paths

PROJECT_ROOT = paths.app_dir()
STATIC_DIR = PROJECT_ROOT / "static"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
MERLIN_BOT_DIR = PROJECT_ROOT / "merlin-bot"

# Add project root to sys.path for module imports
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv(paths.config_path())
load_dotenv(paths.bot_config_path())  # Bot-specific vars (Discord token, etc.)

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")
TUNNEL_ENABLED = os.getenv("TUNNEL_ENABLED", "true").lower() in ("true", "1", "yes")
TUNNEL_TOKEN = os.getenv("TUNNEL_TOKEN", "")
TUNNEL_HOSTNAME = os.getenv("TUNNEL_HOSTNAME", "")

# CWD = where the user launched main.py
CWD = Path.cwd().resolve()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("merlin")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

from auth import (
    _AuthRedirect,
    configure as configure_auth,
    require_auth,
    set_auth_cookie,
    clear_auth_cookie,
)

configure_auth(DASHBOARD_PASS)

# ---------------------------------------------------------------------------
# Nav items
# ---------------------------------------------------------------------------

# Icons (Lucide SVGs)
ICON_FILES = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/></svg>'
ICON_TERMINAL = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m4 17 6-6-6-6"/><path d="M12 19h8"/></svg>'
ICON_COMMITS = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><line x1="12" y1="3" x2="12" y2="9"/><line x1="12" y1="15" x2="12" y2="21"/></svg>'
ICON_NOTES = '&#9998;'

CORE_NAV_ITEMS = [
    {"url": "/files", "icon": ICON_FILES, "label": "Files"},
    {"url": "/terminal", "icon": ICON_TERMINAL, "label": "Terminal"},
    {"url": "/commits", "icon": ICON_COMMITS, "label": "Commits"},
    {"url": "/notes", "icon": ICON_NOTES, "label": "Notes"},
]

# Will be extended by apps
nav_items: list[dict] = list(CORE_NAV_ITEMS)
show_bot_status: bool = False

# Optional dependency availability (set by _check_optional_deps)
TMUX_AVAILABLE: bool = True

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Merlin", docs_url=None, redoc_url=None)


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response


app.add_middleware(NoCacheStaticMiddleware)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _template_context(request: Request, **extra) -> dict:
    """Build template context with nav items included."""
    return {"request": request, "nav_items": nav_items, "show_bot_status": show_bot_status, **extra}


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.exception_handler(_AuthRedirect)
async def _auth_redirect_handler(request: Request, exc: _AuthRedirect):
    return RedirectResponse(url=f"/login?next={quote(exc.next_url)}", status_code=303)


def _safe_next_url(url: str) -> str:
    """Sanitize ?next= redirect target to prevent open redirects."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/files"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/files", error: str = ""):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "next_url": _safe_next_url(next),
        "error": error,
    })


@app.post("/login")
def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/files"),
):
    safe_next = _safe_next_url(next)

    if not DASHBOARD_PASS:
        return RedirectResponse(url=safe_next, status_code=303)

    if not secrets.compare_digest(password.encode(), DASHBOARD_PASS.encode()):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "next_url": safe_next,
            "error": "Wrong password",
        }, status_code=401)

    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response = RedirectResponse(url=safe_next, status_code=303)
    set_auth_cookie(response, DASHBOARD_USER, DASHBOARD_PASS, secure=secure)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_auth_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------


@app.get("/", response_class=RedirectResponse)
def root(_auth=Depends(require_auth)):
    return RedirectResponse(url="/files")


# ---------------------------------------------------------------------------
# Core modules
# ---------------------------------------------------------------------------

from files import router as files_router, FILES_STATIC_DIR
from files.routes import set_cwd as files_set_cwd
from commits import router as commits_router, COMMITS_STATIC_DIR
from commits.git_parser import set_repo_dir
from notes import router as notes_router, NOTES_STATIC_DIR
from terminal import router as terminal_router
from terminal.routes import set_cwd as terminal_set_cwd

# Pass CWD to modules
files_set_cwd(str(CWD))
terminal_set_cwd(str(CWD))
set_repo_dir(str(CWD))

app.include_router(files_router, dependencies=[Depends(require_auth)])
app.include_router(commits_router, dependencies=[Depends(require_auth)])
app.include_router(notes_router, dependencies=[Depends(require_auth)])
app.include_router(terminal_router)  # WebSocket auth handled internally

# Module statics BEFORE general static (more specific path first)
app.mount("/static/files", StaticFiles(directory=str(FILES_STATIC_DIR)), name="files-static")
app.mount("/static/commits", StaticFiles(directory=str(COMMITS_STATIC_DIR)), name="commits-static")
app.mount("/static/notes", StaticFiles(directory=str(NOTES_STATIC_DIR)), name="notes-static")

# ---------------------------------------------------------------------------
# App: merlin-bot (optional)
# ---------------------------------------------------------------------------

bot_plugin = None

try:
    sys.path.insert(0, str(MERLIN_BOT_DIR))
    import merlin_bot as bot_plugin  # type: ignore[no-redef]

    app.include_router(bot_plugin.router, dependencies=[Depends(require_auth)])
    if bot_plugin.STATIC_DIR:
        app.mount("/static/merlin-app", StaticFiles(directory=str(bot_plugin.STATIC_DIR)), name="merlin-app-static")

    # Prepend app nav items before core items
    nav_items[:0] = bot_plugin.NAV_ITEMS
    show_bot_status = True
    logger.info("Merlin Bot plugin loaded — bot + monitoring pages available")
except ImportError:
    logger.info("Merlin Bot not found — running core modules only")

# General static mount (must be last)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Inject nav_items into all template responses
# ---------------------------------------------------------------------------

from starlette.routing import Route, Mount


def _patch_template_responses():
    """Patch all Jinja2Templates instances to include nav_items in context."""
    # Override the Jinja2Templates render to inject nav_items
    import functools

    original_response = Jinja2Templates.TemplateResponse

    @functools.wraps(original_response)
    def patched_response(self, *args, **kwargs):
        # Handle both old-style (name, context) and new-style (request, name) calls
        if args:
            # Find the context dict in args or kwargs
            if len(args) >= 2 and isinstance(args[1], dict):
                ctx = args[1]
                ctx.setdefault("nav_items", nav_items)
                ctx.setdefault("show_bot_status", show_bot_status)
        if "context" in kwargs and isinstance(kwargs["context"], dict):
            kwargs["context"].setdefault("nav_items", nav_items)
            kwargs["context"].setdefault("show_bot_status", show_bot_status)
        return original_response(self, *args, **kwargs)

    Jinja2Templates.TemplateResponse = patched_response


_patch_template_responses()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def _detect_pkg_manager() -> str:
    """Detect the system package manager."""
    for mgr in ("apt", "pacman", "brew"):
        if shutil.which(mgr):
            return mgr
    return ""


def _install_cmd(pkg: str) -> str:
    """Return the install command for a package on the detected package manager."""
    mgr = _detect_pkg_manager()
    cmds = {
        "apt": f"sudo apt install -y {pkg}",
        "pacman": f"sudo pacman -S --noconfirm {pkg}",
        "brew": f"brew install {pkg}",
    }
    return cmds.get(mgr, f"install {pkg} using your package manager")


def _check_optional_deps(tunnel_enabled: bool) -> None:
    """Check optional dependencies and set up graceful degradation."""
    global TMUX_AVAILABLE, TUNNEL_ENABLED

    if not shutil.which("tmux"):
        TMUX_AVAILABLE = False
        cmd = _install_cmd("tmux")
        logger.warning("tmux not found — terminal disabled (install: %s)", cmd)
        # Mark terminal nav item as disabled
        for item in nav_items:
            if item.get("url") == "/terminal":
                item["disabled"] = True
                item["tooltip"] = f"tmux required — install: {cmd}"

    if tunnel_enabled and not shutil.which("cloudflared"):
        cmd = _install_cmd("cloudflared")
        logger.warning("cloudflared not found — tunnel disabled (install: %s)", cmd)
        TUNNEL_ENABLED = False


def _disable_bot_plugin() -> None:
    """Disable the bot plugin at runtime (e.g. missing Discord config)."""
    global bot_plugin, show_bot_status
    bot_plugin = None
    show_bot_status = False
    nav_items[:] = list(CORE_NAV_ITEMS)


def _validate_config(tunnel_enabled: bool) -> None:
    """Validate required configuration. Fails fast with a helpful message."""
    global DASHBOARD_PASS
    env_path = paths.config_path()
    errors: list[str] = []

    if not env_path.exists():
        errors.append(
            f"Config file not found at {env_path}\n"
            f"  Run the setup wizard to create it:\n"
            f"    merlin setup"
        )

    # Fail fast before doing anything else (like generating passwords)
    if errors:
        msg = "Configuration error(s):\n\n" + "\n\n".join(f"  {i+1}. {e}" for i, e in enumerate(errors))
        print(msg, file=sys.stderr)
        raise SystemExit(1)

    if not DASHBOARD_PASS and tunnel_enabled:
        generated = secrets.token_urlsafe(12)
        logger.warning("DASHBOARD_PASS not set — auto-generating password for tunnel security")
        DASHBOARD_PASS = generated
        os.environ["DASHBOARD_PASS"] = generated
        configure_auth(DASHBOARD_PASS)
        print(f"  Auto-generated login: {DASHBOARD_USER} / {generated}")
    elif not DASHBOARD_PASS:
        logger.warning("DASHBOARD_PASS not set — running without auth (local-only is fine)")

    # Check optional deps (warns, doesn't fail)
    _check_optional_deps(tunnel_enabled)

    # Validate bot config if plugin is loaded — degrade gracefully if missing
    if bot_plugin:
        try:
            bot_plugin.validate()
        except SystemExit:
            _disable_bot_plugin()
            logger.warning("Bot disabled — Discord not configured. Run 'merlin setup' to configure.")


def start_server(port: int = 3123, host: str = "0.0.0.0", no_tunnel: bool = False) -> None:
    """Start the Merlin dashboard server. Called by cli.py or main()."""
    import uvicorn

    if no_tunnel:
        global TUNNEL_ENABLED
        TUNNEL_ENABLED = False

    _validate_config(TUNNEL_ENABLED)

    print(f"Merlin starting on http://{host}:{port}")
    print(f"CWD: {CWD}")
    if bot_plugin:
        print("Merlin Bot plugin: loaded")

    async def _notify_tunnel_url(url: str) -> None:
        """Send the tunnel URL to Discord (if merlin-bot is available)."""
        if not bot_plugin:
            logger.info("Tunnel URL: %s (no bot to notify)", url)
            return
        try:
            await bot_plugin.on_tunnel_url(url)
            logger.info("Tunnel URL sent to Discord: %s", url)
        except Exception:
            logger.exception("Could not send tunnel URL to Discord")

    async def _run():
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)

        tasks = [asyncio.create_task(server.serve())]

        if TUNNEL_ENABLED:
            from tunnel import start_tunnel
            tasks.append(asyncio.create_task(start_tunnel(
                port=port,
                tunnel_token=TUNNEL_TOKEN,
                tunnel_hostname=TUNNEL_HOSTNAME,
                on_url=_notify_tunnel_url,
            )))

        if bot_plugin:
            tasks.append(asyncio.create_task(bot_plugin.start()))

        await asyncio.gather(*tasks)

    asyncio.run(_run())


def main():
    parser = argparse.ArgumentParser(
        description="Merlin — Portable mobile dev environment.",
        epilog="""
Examples:
  uv run main.py                    # Start with defaults
  uv run main.py --port 8080        # Custom port
  uv run main.py --no-tunnel        # Local only
  uv run main.py --host 127.0.0.1   # Localhost only

Environment variables (from .env or shell):
  DASHBOARD_USER    Auth username (default: admin)
  DASHBOARD_PASS    Auth password (required for security)
  TUNNEL_ENABLED    Enable Cloudflare tunnel (default: true)
  TUNNEL_TOKEN      Named tunnel token (optional)
  TUNNEL_HOSTNAME   Named tunnel hostname (optional)
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=3123, help="Port to serve on (default: 3123)")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--no-tunnel", action="store_true", help="Disable Cloudflare tunnel")
    args = parser.parse_args()

    start_server(port=args.port, host=args.host, no_tunnel=args.no_tunnel)


if __name__ == "__main__":
    main()
