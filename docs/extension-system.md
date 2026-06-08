# Extension System

Reference documentation for Merlin's extension system. Covers tiers, the extension interface, state management, the registry, and the Extensions/Settings pages.

## Overview

Extensions add functionality to the Merlin dashboard — pages, sidebar nav items, API routes, and lifecycle hooks. The system has three tiers determined by where the code lives, not by declaration.

```
main.py (_load_extension)
  ├─ Core (files, terminal, commits) — always active, registered manually
  ├─ Built-in (notes, merlin-bot) — ship with Merlin, toggleable
  └─ Installed (~/.merlin/extensions/*) — user-installed, toggleable
      └─ each folder is a Python module with a router
```

Key property: **Merlin always starts.** A broken extension is recorded in the registry with an error but never prevents the dashboard from loading.

## Tiers

| Tier | Location | Default | Can disable? | Example |
|------|----------|---------|-------------|---------|
| Core | Hardcoded in `main.py` | Always on | No | files, terminal, commits |
| Built-in | Ships with Merlin source | Per `BUILT_IN_DEFAULTS` | Yes | notes (on), merlin-bot (off) |
| Installed | `~/.merlin/extensions/` | On | Yes | video-scenes |

An extension cannot declare itself as core or built-in. The tier is inferred from where it lives.

## Extension Interface

Every extension can export the following. Only `router` is required.

```python
from fastapi import APIRouter

# Required — FastAPI routes
router = APIRouter()

# Optional — sidebar entry (list, can have multiple)
NAV_ITEMS = [
    {"url": "/scenes", "icon": '<svg .../>', "label": "Scenes"},
]

# Optional — static files directory
STATIC_DIR = Path(__file__).parent / "static"

# Optional — metadata for the Extensions page
EXTENSION_META = {
    "name": "Video Scenes",
    "description": "Browse and select scene candidates",
    "icon": '<svg .../>',
    "config_fields": [
        {
            "key": "DISCORD_BOT_TOKEN",
            "label": "Discord Bot Token",
            "secret": True,
            "required": True,
            "help": "Create at discord.com/developers",
        },
    ],
}

# Optional — lifecycle hooks
def validate():
    """Check config. Raise SystemExit on error."""
    ...

async def start():
    """Async startup (e.g., Discord client)."""
    ...

async def on_tunnel_url(url: str):
    """Called when tunnel URL is available."""
    ...
```

**Defaults when fields are missing:**
- No `EXTENSION_META` → name from folder (titlecased), no description, puzzle piece icon
- No `NAV_ITEMS` → extension has routes but no sidebar entry
- No `STATIC_DIR` → no static files mounted
- No `validate()` → always considered valid
- No `start()` → no async startup
- No `on_tunnel_url()` → not notified
- No `logger` → Merlin injects one automatically (see Logging below)

## Logging

Extensions get a properly namespaced logger under the `merlin.ext.*` tree, which inherits the `RotatingFileHandler` from the root `merlin` logger. Logs appear in `~/.merlin/logs/merlin.log` with the extension name in brackets.

**Automatic injection**: If your extension module doesn't define a `logger` attribute, Merlin injects one at load time. You can use it directly:

```python
# my_extension/__init__.py
from fastapi import APIRouter

router = APIRouter()

# logger is injected by Merlin — no import needed
# It will be: logging.getLogger("merlin.ext.my_extension")

@router.get("/my-page")
def my_page():
    logger.info("Page loaded")  # writes to merlin.log as [merlin.ext.my_extension]
    ...
```

**Explicit creation**: For submodules or when you want to be explicit, use `get_logger()`:

```python
# my_extension/renderer.py
from merlin_ext import get_logger

logger = get_logger("my-extension.renderer")  # → merlin.ext.my_extension.renderer
```

The syntax is the same everywhere — main module or submodule:

```python
from merlin_ext import get_logger

logger = get_logger("my-extension")              # → merlin.ext.my_extension
logger = get_logger("my-extension.renderer")     # → merlin.ext.my_extension.renderer
logger = get_logger("my-extension.renderer.pdf") # → merlin.ext.my_extension.renderer.pdf
```

Dashes are converted to underscores automatically. All loggers inherit the file handler from `merlin`, so they appear in `merlin.log` and respect the same rotation settings.

**Do not** create loggers outside the `merlin.*` namespace — they won't be written to `merlin.log`.

## Templates

Extensions that render HTML should create their `Jinja2Templates` via `make_templates()` from `merlin_ext`. This registers app-wide globals (`nav_items`, `saas_mode`, `saas_api_url`, `extensions_error_count`) so `base.html` renders the sidebar correctly, and appends the project root `templates/` directory as a fallback so `base.html` is reachable without manual path plumbing.

```python
from pathlib import Path
from fastapi import APIRouter, Request
from merlin_ext import make_templates

EXT_DIR = Path(__file__).parent.resolve()
templates = make_templates(EXT_DIR / "templates")

router = APIRouter()

@router.get("/my-page")
def my_page(request: Request):
    return templates.TemplateResponse(request, "page.html", {"foo": "bar"})
```

Use Starlette's new `TemplateResponse(request, name, context)` signature — `request` first, and **do not** include `"request": request` in the context dict (Starlette injects it). The old `TemplateResponse(name, {"request": request, ...})` form still works but emits a deprecation warning and loses type safety.

**Do not** construct `Jinja2Templates` directly in extensions — you'd lose the shared globals and break the sidebar.

## Folder Naming

Extension folders can use hyphens or underscores (`video-scenes` or `video_scenes`). The loader does `name.replace("-", "_")` to produce a valid Python import.

## State Management

**File**: `~/.merlin/extensions.json`

Stores explicit user toggle choices only:

```json
{
    "notes": false,
    "video-scenes": true
}
```

**Resolution logic** (`_resolve_enabled` in `main.py`):
1. In `extensions.json`? → use that value
2. Built-in? → use `BUILT_IN_DEFAULTS` (`notes=True`, `merlin-bot=False`)
3. Installed? → `True` (you put it there, you want it active)
4. Core? → always `True` regardless

A fresh install has an empty `{}`. The file only grows as users toggle things.

## Registry

`main.py` builds an `extension_registry: dict[str, ExtensionInfo]` at startup.

```python
@dataclass
class ExtensionInfo:
    id: str               # Folder name (e.g., "video-scenes")
    tier: str             # "core" | "built-in" | "installed"
    enabled: bool         # User's choice (or default)
    loaded: bool          # Successfully imported?
    error: str | None     # Import/validate error message
    meta: dict            # EXTENSION_META (or generated defaults)
    has_start: bool       # Has async start() hook
    has_tunnel_hook: bool # Has on_tunnel_url() hook
    module: object | None # The imported module (if loaded)
```

The registry is used by:
- `start_server()` — iterates for `start()` and `on_tunnel_url()` hooks
- `_validate_config()` — validates bot config if loaded
- Extensions page — lists all extensions with status
- `register_template_globals()` — publishes `extensions_error_count` to all template instances for the sidebar badge

## Extensions and skills

An **enabled** extension's `skills/` directory feeds the separate **skill
registry** (`lib/skills.py`) — disabling the extension drops its skills on the
next rebuild. `ext_commands.all_extension_states()` reports every extension and
its enabled flag; `enabled_extension_source_dirs()` is the enabled subset the
skill registry aggregates. Precedence is **core > extension > user**, so an
extension can shadow the user home but never a core skill. The Extensions page
audit lists **only that extension's** skills (a per-extension security
surface); core and user skills have no extension row, so they appear in
`merlin skills` instead. Full reference: [`skill-system.md`](skill-system.md).

## Creating an Installed Extension

1. Create a folder in `~/.merlin/extensions/` (e.g., `my-tool/`)
2. Add a Python file matching the folder name with hyphens replaced by underscores (e.g., `my_tool.py`)
3. Export at minimum a `router = APIRouter()`
4. Optionally export `NAV_ITEMS`, `STATIC_DIR`, `EXTENSION_META`
5. Restart Merlin — the extension appears on the Extensions page

Example minimal extension:

```python
"""My Tool — a custom Merlin extension."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

NAV_ITEMS = [
    {"url": "/my-tool", "icon": '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/></svg>', "label": "My Tool"},
]

@router.get("/my-tool", response_class=HTMLResponse)
def my_tool_page(request: Request):
    return "<h1>Hello from My Tool</h1>"
```

## Extensions Page

**Route**: `GET /extensions`

Shows all extensions grouped by tier (Core, Built-in, Installed). Each card shows:
- Icon, name, description
- Lock icon for core (can't toggle) or toggle switch for others
- Error message if import failed (red border)
- Config accordion for extensions with `config_fields`
- Restart banner when changes are made

**API endpoints**:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/extensions` | GET | Extensions page (HTML) |
| `/api/extensions` | GET | List all extensions as JSON |
| `/api/extensions/{id}/toggle` | POST | Enable/disable an extension |
| `/api/extensions/{id}/config` | POST | Save config fields to `config.env` |
| `/api/restart` | POST | Restart Merlin via `restart.sh` |

The config endpoint only accepts keys declared in the extension's `config_fields` metadata. Undeclared keys are silently ignored.

## Settings Page

**Route**: `GET /settings` (accessible from gear dropdown in sidebar header)

Two sections:
- **Authentication** — dashboard password (disabled in SaaS mode: "managed by Merlin Cloud")
- **Voice Transcription** — OpenAI API key (disabled in SaaS mode: "provided by Merlin Cloud")

**API endpoints**:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/settings` | GET | Settings page (HTML) |
| `/api/settings` | GET | Current settings (booleans, never raw secrets) |
| `/api/settings` | POST | Update settings in `config.env` |

## Sidebar

Nav items are built dynamically from enabled extensions:

```
Files           ← core (always)
Terminal        ← core (always)
Commits         ← core (always)
Notes           ← built-in (if enabled)
Bot             ← built-in (if enabled, single entry with tabs)
Video Scenes    ← installed (if enabled)
Extensions      ← always shown (management page, puzzle icon)
```

When any extension has an error, the Extensions nav item shows a small red dot badge.

## merlin-bot as Extension Example

The merlin-bot extension is a good example of a full-featured built-in extension with `EXTENSION_META` and `config_fields`:

```python
EXTENSION_META = {
    "name": "Merlin Bot",
    "description": "Discord AI assistant powered by Claude Code",
    "icon": '<svg .../>',
    "config_fields": [
        {"key": "DISCORD_BOT_TOKEN", "label": "Discord Bot Token", "secret": True, "required": True},
        {"key": "DISCORD_CHANNEL_IDS", "label": "Discord Channel IDs (comma-separated)", "secret": False, "required": True},
    ],
}
```

The `config_fields` metadata enables the Extensions page to render configuration forms for `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_IDS`, saved to `config.env` via the config API endpoint.

Note: merlin-bot is **Discord-only** in scope. It handles message listening, thread creation, voice transcription, and prompt building. Cron scheduling and the Claude wrapper are separate core modules (`cron/` and `lib/claude.py`).

## Key Files

| File | Role |
|------|------|
| `main.py` | ExtensionInfo, registry, loader, state mgmt, API routes |
| `paths.py` | `extensions_dir()`, `extensions_state_path()` |
| `templates/extensions.html` | Extensions management page |
| `templates/settings.html` | Settings page |
| `static/extensions.css` + `.js` | Extensions page UI |
| `static/settings.css` + `.js` | Settings page UI |
| `templates/base.html` | Sidebar nav rendering, error badge, Settings link |
| `notes/__init__.py` | Built-in extension example (exports router, NAV_ITEMS, STATIC_DIR) |
| `merlin-bot/merlin_bot.py` | Built-in extension example (exports router, NAV_ITEMS, EXTENSION_META, start, validate, on_tunnel_url) |
