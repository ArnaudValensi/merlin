"""
Merlin — Portable mobile dev environment.

Launch on any Linux machine to get a web-based development environment.
Served locally; remote access comes from Merlin Cloud (SaaS mode) or a
tunnel/reverse proxy you bring yourself.

Core modules: File browser, Terminal, Commit browser, Notes editor.
Apps: Optional plugins (e.g., merlin-bot) that add pages to the sidebar.

Usage:
    uv run main.py                    # Start on port 3123, CWD = current dir
    uv run main.py --port 8080        # Custom port
    uv run main.py --host 127.0.0.1   # Bind to localhost only
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

import paths

PROJECT_ROOT = paths.app_dir()
STATIC_DIR = PROJECT_ROOT / "static"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
MERLIN_BOT_DIR = PROJECT_ROOT / "merlin-bot"

# Add project root and lib/ to sys.path for module imports
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

from merlin_ext import make_templates, register_template_globals

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv(paths.config_path())
load_dotenv(paths.bot_config_path())  # Bot-specific vars (Discord token, etc.)

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")
MERLIN_SAAS_TOKEN = os.getenv("MERLIN_SAAS_TOKEN", "")
MERLIN_SAAS_API = os.getenv("MERLIN_SAAS_API", "https://merlincloud.dev")

# CWD = where the user launched main.py
CWD = Path.cwd().resolve()

# Capture the launch directory so subprocesses (e.g. job command runs) can
# default to running where Merlin was started. Inherited by child processes
# through the environment. setdefault so an explicit override is respected.
os.environ.setdefault("MERLIN_LAUNCH_CWD", str(CWD))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("merlin")


def _setup_logging() -> None:
    """Configure the unified merlin.* logger hierarchy with rotating file handler."""
    if logger.handlers:
        return  # Already configured
    from logging.handlers import RotatingFileHandler

    log_dir = paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    file_handler = RotatingFileHandler(
        log_dir / "merlin.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

from auth import (
    _AuthRedirect,
    _SaaSAuthRedirect,
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
ICON_NOTES = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 6h4"/><path d="M2 10h4"/><path d="M2 14h4"/><rect x="6" y="2" width="16" height="20" rx="2"/><path d="M10 8h8"/><path d="M10 12h8"/><path d="M10 16h8"/></svg>'
ICON_EXTENSIONS = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19.439 7.85c-.049.322.059.648.289.878l1.568 1.568c.47.47.706 1.087.706 1.704s-.235 1.233-.706 1.704l-1.611 1.611a.98.98 0 0 1-.837.276c-.47-.07-.802-.48-.968-.925a2.501 2.501 0 1 0-3.214 3.214c.446.166.855.497.925.968a.979.979 0 0 1-.276.837l-1.61 1.61a2.404 2.404 0 0 1-1.705.707 2.402 2.402 0 0 1-1.704-.706l-1.568-1.568a1.026 1.026 0 0 0-.877-.29c-.493.074-.84.504-1.02.968a2.5 2.5 0 1 1-3.237-3.237c.464-.18.894-.527.967-1.02a1.026 1.026 0 0 0-.289-.877l-1.568-1.568A2.402 2.402 0 0 1 1.998 12c0-.617.236-1.234.706-1.704L4.315 8.685a.98.98 0 0 1 .837-.276c.47.07.802.48.968.925a2.501 2.501 0 1 0 3.214-3.214c-.446-.166-.855-.497-.925-.968a.979.979 0 0 1 .276-.837l1.61-1.61a2.404 2.404 0 0 1 1.705-.707c.617 0 1.234.236 1.704.706l1.568 1.568c.23.23.556.338.877.29.493-.074.84-.504 1.02-.968a2.5 2.5 0 1 1 3.237 3.237c-.464.18-.894.527-.967 1.02Z"/></svg>'

ICON_JOBS = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>'

CORE_NAV_ITEMS = [
    {"url": "/files", "icon": ICON_FILES, "label": "Files"},
    {"url": "/terminal", "icon": ICON_TERMINAL, "label": "Terminal"},
    {"url": "/commits", "icon": ICON_COMMITS, "label": "Commits"},
    {"url": "/jobs", "icon": ICON_JOBS, "label": "Jobs"},
]

# Extensions nav item — always visible, appended after all extension nav items
EXTENSIONS_NAV_ITEM = {
    "url": "/extensions",
    "icon": ICON_EXTENSIONS,
    "label": "Extensions",
}

# Will be extended by extensions
nav_items: list[dict] = list(CORE_NAV_ITEMS)
show_bot_status: bool = False

# Optional dependency availability (set by _check_optional_deps)
TMUX_AVAILABLE: bool = True

# fd binary name (fdfind on Ubuntu/Debian, fd elsewhere) — set by _check_fd()
FD_BINARY: str = ""

# ---------------------------------------------------------------------------
# Extension system
# ---------------------------------------------------------------------------


@dataclass
class ExtensionInfo:
    id: str  # Folder name (e.g., "video-scenes")
    tier: str  # "core" | "built-in" | "installed"
    enabled: bool  # User's choice (or default)
    loaded: bool  # Successfully imported?
    error: str | None  # Import/validate error message
    meta: dict = field(default_factory=dict)
    module: ModuleType | None = None  # The imported module (if loaded)

    # Hooks resolved at load time — None if the extension doesn't export them.
    # Normalizing the per-module optionality into `Callable | None` fields lets
    # call sites narrow with `is not None` and gives sound type checking.
    # Async hooks return `Coroutine` (not `Awaitable`) so asyncio.create_task accepts them.
    start: Callable[[], Coroutine[Any, Any, None]] | None = None
    validate: Callable[[], None] | None = None
    notify: Callable[..., Any] | None = None


extension_registry: dict[str, ExtensionInfo] = {}

# Built-in extension defaults — single source of truth lives in
# ext_commands.py so the CLI (merlin setup) resolves enabled state the
# same way the server does.
import ext_commands as _ext_commands

BUILT_IN_DEFAULTS: dict[str, bool] = _ext_commands.BUILTIN_DEFAULT_ENABLED


def _load_extensions_state() -> dict:
    """Read extensions.json. Returns {} if file missing or invalid."""
    state_path = paths.extensions_state_path()
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text())
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_extensions_state(state: dict) -> None:
    """Write extensions.json."""
    state_path = paths.extensions_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n")


def _resolve_enabled(ext_id: str, tier: str, state: dict) -> bool:
    """Determine if an extension is enabled.

    Priority: explicit state > built-in defaults > installed default (True).
    Core extensions are always enabled regardless of state.
    """
    if tier == "core":
        return True
    if ext_id in state:
        return bool(state[ext_id])
    if tier == "built-in":
        return BUILT_IN_DEFAULTS.get(ext_id, True)
    # Installed extensions default to enabled
    return True


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

_http_logger = logging.getLogger("merlin.http")


class ErrorLoggingMiddleware(BaseHTTPMiddleware):
    """Log non-2xx/3xx HTTP responses for debugging."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if response.status_code >= 400:
            _http_logger.warning(
                "%s %s → %d", request.method, request.url.path, response.status_code
            )
        return response


app.add_middleware(ErrorLoggingMiddleware)

templates = make_templates(TEMPLATES_DIR)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.exception_handler(_AuthRedirect)
async def _auth_redirect_handler(request: Request, exc: _AuthRedirect):
    return RedirectResponse(url=f"/login?next={quote(exc.next_url)}", status_code=303)


@app.exception_handler(_SaaSAuthRedirect)
async def _saas_auth_redirect_handler(request: Request, exc: _SaaSAuthRedirect):
    return RedirectResponse(url="https://merlincloud.dev", status_code=303)


def _safe_next_url(url: str) -> str:
    """Sanitize ?next= redirect target to prevent open redirects."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/files"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/files", error: str = ""):
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next_url": _safe_next_url(next),
            "error": error,
        },
    )


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
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next_url": safe_next,
                "error": "Wrong password",
            },
            status_code=401,
        )

    secure = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto") == "https"
    )
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
    return RedirectResponse(url="/terminal")


# ---------------------------------------------------------------------------
# SaaS proxy: /api/environments
# ---------------------------------------------------------------------------


@app.get("/api/environments")
async def api_environments(_auth=Depends(require_auth)):
    """Proxy environment list from the portal API (avoids CORS)."""
    if not MERLIN_SAAS_TOKEN:
        return []
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{MERLIN_SAAS_API}/api/environments",
                headers={"Authorization": f"Bearer {MERLIN_SAAS_TOKEN}"},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Extensions API
# ---------------------------------------------------------------------------


@app.get("/extensions", response_class=HTMLResponse)
def extensions_page(request: Request, _auth=Depends(require_auth)):
    """Extensions management page."""
    exts = _build_extensions_list()
    return templates.TemplateResponse(request, "extensions.html", {"extensions": exts})


@app.get("/api/extensions")
def api_extensions(_auth=Depends(require_auth)):
    """List all extensions with status."""
    return _build_extensions_list()


@app.post("/api/extensions/{ext_id}/toggle")
def api_toggle_extension(ext_id: str, _auth=Depends(require_auth)):
    """Toggle an extension's enabled state."""
    from fastapi.responses import JSONResponse

    info = extension_registry.get(ext_id)
    if not info:
        return JSONResponse({"detail": "Extension not found"}, status_code=404)
    if info.tier == "core":
        return JSONResponse(
            {"detail": "Core extensions cannot be toggled"}, status_code=400
        )

    state = _load_extensions_state()
    new_enabled = not info.enabled
    state[ext_id] = new_enabled
    _save_extensions_state(state)
    info.enabled = new_enabled  # Update in-memory state for consistent API
    return {"id": ext_id, "enabled": new_enabled}


def _read_config_env() -> dict[str, str]:
    """Read config.env into a dict."""
    cfg_path = paths.config_path()
    result = {}
    if cfg_path.exists():
        for line in cfg_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    return result


def _write_config_env(data: dict[str, str]) -> None:
    """Write dict to config.env with 0600 permissions."""
    cfg_path = paths.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("\n".join(f"{k}={v}" for k, v in data.items()) + "\n")
    cfg_path.chmod(0o600)


@app.post("/api/extensions/{ext_id}/config")
async def api_save_extension_config(
    ext_id: str, request: Request, _auth=Depends(require_auth)
):
    """Save extension config fields to config.env."""
    from fastapi.responses import JSONResponse

    info = extension_registry.get(ext_id)
    if not info:
        return JSONResponse({"detail": "Extension not found"}, status_code=404)

    # Only allow keys declared in the extension's config_fields
    allowed_keys = set()
    if info.meta.get("config_fields"):
        allowed_keys = {f["key"] for f in info.meta["config_fields"]}

    body = await request.json()
    existing = _read_config_env()
    for key, value in body.items():
        if key not in allowed_keys:
            continue
        if value:
            existing[key] = value
        else:
            existing.pop(key, None)
    _write_config_env(existing)

    masked = {
        key: ("***" if existing.get(key, "") else "")
        for key in body
        if key in allowed_keys
    }
    return {"ok": True, "values": masked}


@app.post("/api/restart")
def api_restart(_auth=Depends(require_auth)):
    """Restart Merlin via restart.sh."""
    import subprocess

    restart_script = paths.app_dir() / "restart.sh"
    if restart_script.exists():
        subprocess.Popen(["bash", str(restart_script)], start_new_session=True)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Version API
# ---------------------------------------------------------------------------

# Cached latest tag: (version_string, timestamp)
_latest_tag_cache: tuple[str | None, float] = (None, 0.0)
_CACHE_TTL = 3600  # 1 hour


def _get_latest_tag_cached() -> str | None:
    """Fetch latest GitHub tag with 1h in-memory cache."""
    import time

    global _latest_tag_cache
    now = time.monotonic()
    cached_tag, cached_at = _latest_tag_cache
    if cached_tag is not None and (now - cached_at) < _CACHE_TTL:
        return cached_tag

    from cli import fetch_latest_tag

    tag = fetch_latest_tag()
    if tag is not None:
        _latest_tag_cache = (tag, now)
    return tag


@app.get("/api/version")
def api_version(_auth=Depends(require_auth)):
    """Current and latest version info."""
    from cli import get_version

    current = get_version()
    latest = _get_latest_tag_cached()
    update_available = False
    if latest and current not in ("dev", "unknown"):
        # Compare just the base version (strip git describe suffix like -3-gabcdef)
        base = current.split("-")[0] if "-" in current else current
        update_available = latest != base

    return {
        "current": current,
        "latest": latest,
        "update_available": update_available,
        "dev_mode": paths.is_dev_mode(),
    }


@app.post("/api/update")
def api_update(_auth=Depends(require_auth)):
    """Update Merlin to latest version and restart."""
    import subprocess

    from cli import (
        atomic_symlink,
        download_and_extract,
        fetch_latest_tag,
        get_version,
    )

    if paths.is_dev_mode():
        return {"ok": False, "error": "Update not available in dev mode"}

    latest = fetch_latest_tag()
    if latest is None:
        return {"ok": False, "error": "Could not fetch latest version"}

    current = get_version()
    base = current.split("-")[0] if "-" in current else current
    if latest == base:
        return {"ok": False, "error": f"Already up to date ({latest})"}

    # Download and switch
    versions_dir = paths.merlin_home() / "versions"
    version_dir = versions_dir / latest
    if not version_dir.exists():
        download_and_extract(latest, version_dir)
    atomic_symlink(version_dir, paths.merlin_home() / "current")

    # Invalidate version cache
    global _latest_tag_cache
    _latest_tag_cache = (None, 0.0)

    # Trigger restart
    restart_script = paths.app_dir() / "restart.sh"
    if restart_script.exists():
        subprocess.Popen(["bash", str(restart_script)], start_new_session=True)

    return {"ok": True, "version": latest}


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, _auth=Depends(require_auth)):
    """Settings page."""
    cfg = _read_config_env()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "openai_key_set": bool(cfg.get("OPENAI_API_KEY")),
            "default_public_url": job_webhook.discovered_public_base()[0],
        },
    )


@app.get("/api/settings")
def api_get_settings(_auth=Depends(require_auth)):
    """Current settings state — booleans + masked values, never raw secrets."""
    cfg = _read_config_env()

    # Engine info
    from lib.engine import _registry, get_engine

    engine_name = cfg.get("AGENT_ENGINE", "claude-code")
    try:
        engine = get_engine(engine_name)
        engine_valid = engine.validate() is None
    except ValueError:
        engine_valid = False

    public_base, public_source = job_webhook.resolve_public_base()

    from lib import skills

    return {
        "password_set": bool(cfg.get("DASHBOARD_PASS")),
        "openai_key_set": bool(cfg.get("OPENAI_API_KEY")),
        "agent_engine": engine_name,
        "agent_engine_valid": engine_valid,
        "available_engines": sorted(_registry.keys()),
        "saas_mode": bool(MERLIN_SAAS_TOKEN),
        "public_url": cfg.get("MERLIN_DASHBOARD_URL", ""),
        "effective_public_url": public_base,
        "public_url_source": public_source,
        "default_public_url": job_webhook.discovered_public_base()[0],
        "agent_state_hooks": skills.agent_state_hooks_mode(),
    }


def _normalize_public_url(value: str) -> str:
    """Normalize an operator-entered public base URL.

    Reuses the shared read-side normalizer (scheme-less gets http://, trailing
    slash stripped) and adds the write-side check: reject input that normalizes
    to no host at all.
    """
    from urllib.parse import urlsplit

    value = job_webhook.normalize_base_url(value)
    if value and not urlsplit(value).netloc:
        raise HTTPException(status_code=422, detail="Invalid public URL")
    return value


@app.post("/api/settings")
def api_save_settings(body: dict = Body(...), _auth=Depends(require_auth)):
    """Update settings in config.env.

    Sync ``def`` on purpose: it resolves the public URL, which can make a
    blocking whoami HTTP call, so FastAPI must run it in the threadpool and
    never on the event loop (see portal/CODE_STYLE.md 'Never Block the Event
    Loop'; the 2026-07-02 terminal outage is the cautionary tale).
    """
    cfg = _read_config_env()

    password_changed = False
    for key, value in body.items():
        if key not in (
            "DASHBOARD_PASS",
            "OPENAI_API_KEY",
            "AGENT_ENGINE",
            "MERLIN_DASHBOARD_URL",
        ):
            continue
        if key == "MERLIN_DASHBOARD_URL":
            value = _normalize_public_url(value or "")
        if value:
            if key == "DASHBOARD_PASS" and cfg.get(key) != value:
                password_changed = True
            cfg[key] = value
        elif key != "AGENT_ENGINE":
            # Don't delete AGENT_ENGINE when empty — it has a default
            cfg.pop(key, None)

    # Agent-state pill hooks consent (auto|ask|off). Validated against the
    # single source of truth in lib/skills and folded into the same write.
    from lib import skills

    if "AGENT_STATE_HOOKS" in body:
        mode = (body.get("AGENT_STATE_HOOKS") or "").strip().lower()
        if mode not in skills.AGENT_STATE_HOOKS_MODES:
            raise HTTPException(
                status_code=422, detail="Invalid agent-state-hooks mode"
            )
        cfg[skills.AGENT_STATE_HOOKS_KEY] = mode

    _write_config_env(cfg)

    # Apply the consent choice now: install/refresh on auto, remove on off.
    # `ask` only persists (the dashboard banner drives any sync). Never blocks.
    if "AGENT_STATE_HOOKS" in body:
        try:
            skills.sync_interactive_hooks()
        except Exception:
            logger.warning("agent-state hook sync failed", exc_info=True)

    # Reconfigure auth if password changed
    if password_changed:
        configure_auth(cfg.get("DASHBOARD_PASS", ""))

    # Apply the public URL to the running process: config.env is loaded with
    # setdefault semantics (existing env wins), so without this the change
    # would look inert until the next restart.
    if "MERLIN_DASHBOARD_URL" in body:
        if cfg.get("MERLIN_DASHBOARD_URL"):
            os.environ["MERLIN_DASHBOARD_URL"] = cfg["MERLIN_DASHBOARD_URL"]
        else:
            os.environ.pop("MERLIN_DASHBOARD_URL", None)

    public_base, public_source = job_webhook.resolve_public_base()

    return {
        "ok": True,
        "password_set": bool(cfg.get("DASHBOARD_PASS")),
        "openai_key_set": bool(cfg.get("OPENAI_API_KEY")),
        "restart_required": password_changed,
        "public_url": cfg.get("MERLIN_DASHBOARD_URL", ""),
        "effective_public_url": public_base,
        "public_url_source": public_source,
        "default_public_url": job_webhook.discovered_public_base()[0],
    }


@app.get("/api/agent-state-hooks")
def api_agent_state_hooks_status(_auth=Depends(require_auth)):
    """Consent state for the tmux agent-state pill hook.

    `pending` is true only in `ask` mode with real drift (not installed, or an
    update changed the shipped hook) — that is when the dashboard banner asks.
    """
    from lib import skills

    mode = skills.agent_state_hooks_mode()
    pending = mode == "ask" and skills.interactive_hooks_drift()
    return {"mode": mode, "pending": pending}


@app.post("/api/agent-state-hooks")
def api_agent_state_hooks_consent(body: dict = Body(...), _auth=Depends(require_auth)):
    """Apply a consent-banner choice.

    always    -> mode auto, install now       (yes, and keep it updated)
    once      -> stay ask, install now        (yes, just this once)
    not_now   -> stay ask, do nothing         (ask me again later)
    never     -> mode off, remove Merlin's    (no, stop asking)
    """
    from lib import skills

    choice = (body.get("choice") or "").strip().lower()
    if choice == "always":
        skills.set_agent_state_hooks_mode("auto")
        changed = skills.install_interactive_hooks()
    elif choice == "once":
        changed = skills.install_interactive_hooks()
    elif choice == "not_now":
        changed = False
    elif choice == "never":
        skills.set_agent_state_hooks_mode("off")
        changed = skills.remove_interactive_hooks()
    else:
        raise HTTPException(status_code=422, detail="Invalid consent choice")

    mode = skills.agent_state_hooks_mode()
    pending = mode == "ask" and skills.interactive_hooks_drift()
    return {"ok": True, "mode": mode, "pending": pending, "changed": changed}


def _extension_audit(info: ExtensionInfo) -> tuple[list[dict], list[dict]]:
    """Skills and commands an extension ships — the security audit surface.

    Both are code-equivalent (skills instruct the engine, commands execute),
    so the Extensions page lists them read-only per extension.
    """
    if info.tier == "built-in":
        ext_root = paths.app_dir() / info.id
    elif info.tier == "installed":
        ext_root = paths.extensions_dir() / info.id
    else:
        return [], []

    import ext_commands
    from lib import skills

    skill_list = [
        {"name": spec.name, "description": spec.description}
        for spec in skills.list_source_skills(info.id, ext_root / "skills")
    ]
    command_list = [
        {
            "name": name,
            "invocation": f"merlin {info.id} {name}",
            "help": ext_commands.extract_help(file) or "",
        }
        for name, file in ext_commands.list_commands(ext_root).items()
    ]
    return skill_list, command_list


def _build_extensions_list() -> list[dict]:
    """Build extension list for API/template use."""
    config_env = _read_config_env()
    result = []
    for info in extension_registry.values():
        # Get first nav icon if available
        nav_icon = None
        items = getattr(info.module, "NAV_ITEMS", None)
        if items:
            nav_icon = items[0].get("icon")

        meta = dict(info.meta)
        # Enrich config_fields with current values from config.env
        if "config_fields" in meta:
            enriched = []
            for field in meta["config_fields"]:
                field = dict(field)
                value = config_env.get(field["key"], "")
                field["has_value"] = bool(value)
                if field.get("secret") and value:
                    field["current_value"] = "••••••••"
                else:
                    field["current_value"] = value
                # Resolve dynamic placeholder from callable
                placeholder_fn = field.pop("placeholder_fn", None)
                if callable(placeholder_fn) and not field.get("placeholder"):
                    field["placeholder"] = placeholder_fn()
                enriched.append(field)
            meta["config_fields"] = enriched

        skill_list, command_list = _extension_audit(info)

        ext_data = {
            "id": info.id,
            "tier": info.tier,
            "enabled": info.enabled,
            "loaded": info.loaded,
            "error": info.error,
            "meta": meta,
            "nav_icon": nav_icon,
            "skills": skill_list,
            "commands": command_list,
        }
        result.append(ext_data)
    return result


# ---------------------------------------------------------------------------
# Module mounting — the framework owns namespacing and auth
# ---------------------------------------------------------------------------


def mount_module(
    module: object, module_id: str, static_name: str | None = None
) -> None:
    """Mount a module under the framework-owned namespaces.

    Merlin, not the module, decides where a module's routes live and how they
    are guarded. A module declares intent-scoped routers with no prefixes and
    the framework mounts them:

    - ``api_router``  → ``/api/{slug}``, wrapped in ``require_auth``
    - ``page_router`` → ``/{slug}``, wrapped in ``require_auth``
    - ``STATIC_DIR``  → ``/static/{static_name or module_id}`` — statics are
      keyed by the module id (or ``static_name`` override), NOT the slug, so a
      module's assets stay put even if its ``URL_SLUG`` differs from its id
    - ``register_routes(app)`` — the escape hatch: the module registers
      anything the contract can't express (WebSockets, ...) directly on the
      app and OWNS the path AND the auth for whatever it registers. Its use is
      logged at startup so auth-bypassing routes stay auditable at a glance.

    ``slug`` is ``URL_SLUG`` if the module declares one, else ``module_id``.

    This is the single wiring path for both core modules (hand-imported in
    ``main.py``) and extensions (via the loader), so their wiring is identical.
    """
    slug = getattr(module, "URL_SLUG", module_id)
    mounted_something = False

    api_router = getattr(module, "api_router", None)
    if api_router is not None:
        app.include_router(
            api_router,
            prefix=f"/api/{slug}",
            dependencies=[Depends(require_auth)],
        )
        mounted_something = True

    page_router = getattr(module, "page_router", None)
    if page_router is not None:
        app.include_router(
            page_router,
            prefix=f"/{slug}",
            dependencies=[Depends(require_auth)],
        )
        mounted_something = True

    static_dir = getattr(module, "STATIC_DIR", None)
    if static_dir:
        mount_name = static_name or module_id
        app.mount(
            f"/static/{mount_name}",
            StaticFiles(directory=str(static_dir)),
            name=f"{mount_name}-static",
        )
        mounted_something = True

    register_routes = getattr(module, "register_routes", None)
    if register_routes is not None:
        register_routes(app)
        mounted_something = True
        # Auditable at a glance: everything else under /api/{slug} and /{slug}
        # is authed by the framework; this is the one place a module takes
        # ownership of its own paths and auth.
        logger.info(
            "Module '%s' used register_routes(app) escape hatch "
            "(owns its own path + auth for self-registered routes)",
            module_id,
        )

    # A module still on the pre-migration contract exports a bare `router`,
    # which is no longer recognized — it would otherwise load as healthy while
    # serving nothing. Surface that loudly instead of failing silently. (A
    # commands/skills-only extension legitimately contributes no routes and has
    # no `router`, so it stays quiet.)
    if not mounted_something and getattr(module, "router", None) is not None:
        logger.warning(
            "Module '%s' exposes a legacy `router` attribute but no "
            "api_router/page_router/register_routes, so it serves NO routes. "
            "Migrate it to the api_router/page_router contract.",
            module_id,
        )


# ---------------------------------------------------------------------------
# Core modules
# ---------------------------------------------------------------------------

import files
from files.routes import set_cwd as files_set_cwd
import commits
from commits.routes import set_startup_cwd as commits_set_startup_cwd
import terminal
from terminal.routes import set_cwd as terminal_set_cwd

# Pass CWD to modules
files_set_cwd(str(CWD))
terminal_set_cwd(str(CWD))
commits_set_startup_cwd(str(CWD))

mount_module(files, "files")  # /api/files + /files + /static/files, authed
mount_module(commits, "commits")  # /api/commits + /commits + /static/commits
# terminal is now a normal module: /api/terminal + /terminal authed by the
# framework, and its /ws/terminal WebSocket wired via register_routes(app).
mount_module(terminal, "terminal")

# Job API + page — URL_SLUG="jobs" maps api_router → /api/jobs, page_router → /jobs
import job.routes as job_routes

mount_module(job_routes, "job")

# Session viewer — core module. Transcripts under logs/raw-sessions/ are
# written by lib/ for every caller (jobs, bot, terminal), so viewing one is
# shared infra, not bot-specific. URL_SLUG="session" → /session + /api/session.
import sessions

mount_module(sessions, "sessions")

# Sessions board — core module. A 2D overview of parallel agent sessions built
# on the @agent_state tmux pills. URL_SLUG="board" → /board + /api/board.
import board

mount_module(board, "board")

# Webhooks front desk — intentionally mounted WITHOUT require_auth (terminal
# precedent): /webhooks/* is public and self-authenticating via per-hook
# secrets, verified inside the module. Everything under /api stays gated.
import webhooks

app.include_router(webhooks.router)

# The job module's webhook trigger: core modules register directly.
from job import webhook as job_webhook

webhooks.register("job", job_webhook.resolve)

# Module statics are mounted by mount_module() BEFORE the general /static mount
# below (more specific path first), so no per-module app.mount() is needed here.

# Register core modules in extension registry
extension_registry["files"] = ExtensionInfo(
    id="files",
    tier="core",
    enabled=True,
    loaded=True,
    error=None,
    meta={"name": "Files", "description": "File browser with code viewer"},
)
extension_registry["terminal"] = ExtensionInfo(
    id="terminal",
    tier="core",
    enabled=True,
    loaded=True,
    error=None,
    meta={"name": "Terminal", "description": "Web terminal (tmux)"},
)
extension_registry["commits"] = ExtensionInfo(
    id="commits",
    tier="core",
    enabled=True,
    loaded=True,
    error=None,
    meta={"name": "Commits", "description": "Git commit browser with diffs"},
)

# ---------------------------------------------------------------------------
# Extension loader (built-in + installed)
# ---------------------------------------------------------------------------

_extensions_with_errors: int = 0
_ext_state = _load_extensions_state()


def _load_extension(
    ext_id: str, tier: str, module_loader, static_name: str | None = None
) -> None:
    """Load a single extension into the registry and wire it up if enabled."""
    global _extensions_with_errors, show_bot_status

    enabled = _resolve_enabled(ext_id, tier, _ext_state)

    if not enabled:
        extension_registry[ext_id] = ExtensionInfo(
            id=ext_id,
            tier=tier,
            enabled=False,
            loaded=False,
            error=None,
        )
        logger.info(f"Extension disabled: {ext_id}")
        return

    try:
        mod = module_loader()
    except Exception as e:
        _extensions_with_errors += 1
        extension_registry[ext_id] = ExtensionInfo(
            id=ext_id,
            tier=tier,
            enabled=True,
            loaded=False,
            error=str(e),
        )
        logger.warning(f"Extension {ext_id} failed to load: {e}")
        return

    # Inject a properly namespaced logger so extensions get it for free
    if not hasattr(mod, "logger"):
        from merlin_ext import get_logger as _ext_logger

        mod.logger = _ext_logger(ext_id)

    # Wire up routers + statics through the shared framework helper: api_router
    # → /api/{slug}, page_router → /{slug} (both authed), STATIC_DIR → /static,
    # and register_routes(app) for anything the contract can't express.
    mount_module(mod, ext_id, static_name=static_name)

    # Collect nav items
    ext_nav = getattr(mod, "NAV_ITEMS", [])
    if ext_nav:
        nav_items.extend(ext_nav)

    # Register webhook resolvers with the front desk. An extension's
    # api_router/page_router are auto-authed by mount_module(), so registering
    # a handler here is the idiomatic way to expose a public, self-authenticating
    # webhook without reaching for the register_routes(app) escape hatch.
    ext_hooks = getattr(mod, "WEBHOOK_HANDLERS", None)
    if ext_hooks:
        for hook_source, hook_resolver in ext_hooks.items():
            webhooks.register(hook_source, hook_resolver)

    # Track bot status
    if ext_id == "merlin-bot":
        show_bot_status = True

    # Build metadata
    meta = getattr(mod, "EXTENSION_META", {})
    if not meta.get("name"):
        meta["name"] = ext_id.replace("-", " ").title()

    info = ExtensionInfo(
        id=ext_id,
        tier=tier,
        enabled=True,
        loaded=True,
        error=None,
        meta=meta,
        module=mod,
        start=getattr(mod, "start", None),
        validate=getattr(mod, "validate", None),
        notify=getattr(mod, "notify", None),
    )
    extension_registry[ext_id] = info
    logger.info(f"Extension loaded: {ext_id}")


# --- Built-in: Notes ---
def _load_notes():
    import notes

    return notes


_load_extension("notes", "built-in", _load_notes)

# --- Built-in: Merlin Bot ---
# merlin-bot/ must be on sys.path regardless of enabled state (transcribe.py lives there)
sys.path.insert(0, str(MERLIN_BOT_DIR))


def _load_bot():
    import merlin_bot as _bot

    return _bot


_load_extension("merlin-bot", "built-in", _load_bot, static_name="merlin-app")

# --- Installed extensions (~/.merlin/extensions/) ---


def _load_installed_extensions() -> None:
    """Load installed extensions, rejecting reserved directory names.

    A directory named after a core command or built-in extension would
    silently shadow it at CLI dispatch — fail fast with a visible error
    instead (the extension shows as errored on the Extensions page).
    """
    global _extensions_with_errors
    import ext_commands

    extensions_dir = paths.extensions_dir()
    if not extensions_dir.is_dir():
        return

    for ext_dir in sorted(extensions_dir.iterdir()):
        if not ext_dir.is_dir():
            continue
        ext_name = ext_dir.name

        if ext_name in ext_commands.reserved_names():
            _extensions_with_errors += 1
            extension_registry[ext_name] = ExtensionInfo(
                id=ext_name,
                tier="installed",
                enabled=False,
                loaded=False,
                error=(
                    f"Extension name '{ext_name}' is reserved by a core "
                    "command or built-in extension. Rename the directory "
                    f"{ext_dir}."
                ),
            )
            logger.warning(
                "Installed extension '%s' rejected: name is reserved", ext_name
            )
            continue

        def _make_loader(d=ext_dir, n=ext_name):
            def _loader():
                sys.path.insert(0, str(d))
                return __import__(n.replace("-", "_"))

            return _loader

        _load_extension(ext_name, "installed", _make_loader())


_load_installed_extensions()


def _skill_source_dirs() -> dict[str, Path]:
    """Extension roots that contribute skills: enabled built-ins + installed."""
    sources: dict[str, Path] = {}
    for info in extension_registry.values():
        if not info.loaded:
            continue
        if info.tier == "built-in":
            sources[info.id] = paths.app_dir() / info.id
        elif info.tier == "installed":
            sources[info.id] = paths.extensions_dir() / info.id
    return sources


# Latest agent-state hook reconcile status, read by the dashboard consent
# banner. "pending" means ask-mode drift the user should be asked about.
_agent_state_hooks_status: str = "in-sync"


def _rebuild_skill_registry() -> None:
    """Build the skill registry, canonical aggregation, and user shims."""
    from lib import skills

    try:
        skills.rebuild(_skill_source_dirs())
        # Interactive shims: expose the same skills to the user's own
        # terminal agents (automatic, refreshed every startup)
        skills.sync_interactive_shims()
        logger.info(
            "Skill shims refreshed in %s and %s",
            skills.claude_skills_dir(),
            skills.agents_skills_dir(),
        )
    except Exception:
        logger.warning("Skill registry rebuild failed", exc_info=True)

    # Agent-state pill hooks: reconcile ~/.claude/settings.json per the consent
    # mode (auto installs/updates, off removes, ask only detects drift). Kept
    # separate from the shim sync so a settings.json issue can never stop the
    # skills from refreshing. Never blocks startup.
    global _agent_state_hooks_status
    try:
        _agent_state_hooks_status = skills.sync_interactive_hooks()
        logger.info("Agent-state hooks: %s", _agent_state_hooks_status)
    except Exception:
        logger.warning("Agent-state hook sync failed", exc_info=True)


# Extensions nav item — always last in nav, before sidebar footer
nav_items.append(EXTENSIONS_NAV_ITEM)

# General static mount (must be last)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Register app-wide template globals
# ---------------------------------------------------------------------------

# nav_items is a mutable list — further appends are visible through the reference.
# Scalar values are captured at this point (they don't change after startup).
register_template_globals(
    nav_items=nav_items,
    saas_mode=bool(MERLIN_SAAS_TOKEN),
    saas_api_url=MERLIN_SAAS_API,
    extensions_error_count=_extensions_with_errors,
)


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


def _check_fd() -> None:
    """Check that fd is installed. Hard requirement — exit if missing."""
    global FD_BINARY
    fd = shutil.which("fdfind") or shutil.which("fd")
    if not fd:
        print(
            "Error: fd is not installed\n"
            "\n"
            "    Ubuntu/Debian:  sudo apt install fd-find\n"
            "    Other systems:  https://github.com/sharkdp/fd",
            file=sys.stderr,
        )
        sys.exit(1)
    FD_BINARY = Path(fd).name


def _check_optional_deps() -> None:
    """Check optional dependencies and set up graceful degradation."""
    global TMUX_AVAILABLE

    if not shutil.which("tmux"):
        TMUX_AVAILABLE = False
        cmd = _install_cmd("tmux")
        logger.warning("tmux not found — terminal disabled (install: %s)", cmd)
        # Mark terminal nav item as disabled
        for item in nav_items:
            if item.get("url") == "/terminal":
                item["disabled"] = True
                item["tooltip"] = f"tmux required — install: {cmd}"


def _disable_bot_extension() -> None:
    """Disable the bot extension at runtime (e.g. missing Discord config)."""
    global show_bot_status
    show_bot_status = False
    bot_info = extension_registry.get("merlin-bot")
    if bot_info and bot_info.module:
        # Collect bot nav URLs before clearing
        bot_nav_urls = {
            item.get("url") for item in getattr(bot_info.module, "NAV_ITEMS", [])
        }
        nav_items[:] = [i for i in nav_items if i.get("url") not in bot_nav_urls]
        bot_info.enabled = False
        bot_info.loaded = False
        bot_info.module = None
        bot_info.start = None
        bot_info.validate = None
        bot_info.notify = None


def _validate_config() -> None:
    """Validate required configuration. Fails fast with a helpful message."""
    env_path = paths.config_path()
    errors: list[str] = []

    if not env_path.exists() and not MERLIN_SAAS_TOKEN:
        errors.append(
            f"Config file not found at {env_path}\n"
            f"  Run the setup wizard to create it:\n"
            f"    merlin setup"
        )

    # Fail fast before doing anything else (like generating passwords)
    if errors:
        msg = "Configuration error(s):\n\n" + "\n\n".join(
            f"  {i + 1}. {e}" for i, e in enumerate(errors)
        )
        print(msg, file=sys.stderr)
        raise SystemExit(1)

    if not DASHBOARD_PASS:
        logger.warning(
            "DASHBOARD_PASS not set — running without auth (local-only is fine)"
        )

    # fd is a hard requirement
    _check_fd()

    # Check optional deps (warns, doesn't fail)
    _check_optional_deps()

    # Validate bot config if extension is loaded — degrade gracefully if missing
    bot_info = extension_registry.get("merlin-bot")
    if bot_info and bot_info.validate is not None:
        try:
            bot_info.validate()
        except SystemExit:
            _disable_bot_extension()
            logger.warning(
                "Bot disabled — Discord not configured. Run 'merlin setup' to configure."
            )


def start_server(port: int = 3123, host: str = "0.0.0.0") -> None:
    """Start the Merlin dashboard server. Called by cli.py or main()."""
    import uvicorn

    # Expose the bound port so IP-based public URLs (job webhooks) are exact —
    # in this process via the env var, and to CLI processes via a small file.
    os.environ["MERLIN_PORT"] = str(port)
    try:
        port_file = paths.server_port_path()
        port_file.parent.mkdir(parents=True, exist_ok=True)
        port_file.write_text(str(port))
    except OSError:
        logger.debug("Could not persist server port", exc_info=True)

    _setup_logging()

    # Clean up old log files at startup
    try:
        from structured_log import cleanup_old_logs

        cleanup_old_logs()
    except Exception:
        logger.warning("Failed to clean up old logs", exc_info=True)

    _validate_config()

    # Aggregate skills from enabled extensions (rebuilt every startup so
    # disabled extensions' skills disappear)
    _rebuild_skill_registry()

    from structured_log import log_event

    extensions_loaded = [eid for eid, info in extension_registry.items() if info.loaded]
    log_event(
        "app_started", host=host, port=port, cwd=str(CWD), extensions=extensions_loaded
    )

    print(f"Merlin starting on http://{host}:{port}")
    print(f"CWD: {CWD}")
    bot_info = extension_registry.get("merlin-bot")
    if bot_info and bot_info.loaded:
        print("Merlin Bot extension: loaded")

    async def _run():
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)

        tasks = [asyncio.create_task(server.serve())]

        if MERLIN_SAAS_TOKEN:
            from saas_tunnel import start_saas_tunnel

            tasks.append(
                asyncio.create_task(
                    start_saas_tunnel(
                        token=MERLIN_SAAS_TOKEN,
                        local_port=port,
                    )
                )
            )

            # Start SSH server in SaaS mode (container-side, localhost only)
            from ssh_server import start_ssh_server, stop_ssh_server

            await start_ssh_server()

        # Start the job scheduler (core feature, always runs)
        import job

        await job.start()

        # Start all extensions with start() hooks
        for info in extension_registry.values():
            if info.start is not None:
                tasks.append(asyncio.create_task(info.start()))

        try:
            await asyncio.gather(*tasks)
        finally:
            # Cleanup SSH server on shutdown
            if MERLIN_SAAS_TOKEN:
                await stop_ssh_server()
            log_event("app_stopped")

    asyncio.run(_run())


def main():
    parser = argparse.ArgumentParser(
        description="Merlin — Portable mobile dev environment.",
        epilog="""
Examples:
  uv run main.py                    # Start with defaults
  uv run main.py --port 8080        # Custom port
  uv run main.py --host 127.0.0.1   # Localhost only

Environment variables (from .env or shell):
  DASHBOARD_USER    Auth username (default: admin)
  DASHBOARD_PASS    Auth password (required for security)
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--port", type=int, default=3123, help="Port to serve on (default: 3123)"
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )
    args = parser.parse_args()

    start_server(port=args.port, host=args.host)


if __name__ == "__main__":
    main()
