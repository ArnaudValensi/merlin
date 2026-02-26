# File Browser — Thinking & Context

## Research Summary

### Dashboard Module Pattern

Every dashboard module follows the same structure (commits, notes, terminal):

```
module/
├── __init__.py       # Exports router + STATIC_DIR
├── routes.py         # APIRouter with pages + API endpoints
├── templates/        # Jinja2 templates extending base.html
└── static/           # CSS + JS
```

Registration in `dashboard.py`:
```python
from module import router as module_router, MODULE_STATIC_DIR
app.include_router(module_router, dependencies=[Depends(require_auth)])
app.mount("/static/module", StaticFiles(directory=str(MODULE_STATIC_DIR)), name="module-static")
```

Nav link added to `templates/base.html` sidebar.

### Commits Module as Reference

The commits module is the closest pattern to follow:
- **SPA-style**: single template, multiple views toggled via JS `display: none`
- **URL routing**: `history.pushState`, `popstate` handler, `routeFromUrl()` on load
- **File rendering**: `<table>` with line numbers, highlight.js for syntax coloring
- **Mobile**: edge-to-edge code blocks, hidden metadata, 768px breakpoint
- **Key file**: `merlin-bot/commits/static/commits.js` — 600+ lines, well-structured IIFE

### Notes Module — File Serving Pattern

Notes already serves files from the filesystem:
```python
# notes/routes.py line 80-89
return FileResponse(media_path)  # serves from memory/media/
```

Security: path traversal check via `.resolve()` + bounds check.

### Existing File Serving in the System

- Notes: `GET /notes/media/{path}` — serves `memory/media/` files
- Commits: `GET /api/commits/{hash}/file/{path}` — serves file at specific git commit
- Static files: `StaticFiles` mounts for CSS/JS
- No general-purpose filesystem browser exists

### Dashboard Architecture Conventions

From `docs/dashboard-architecture.md`:

**CSS Variables** (never hardcode colors):
```
--bg-primary, --bg-secondary, --bg-card, --bg-hover, --border
--text-primary, --text-secondary, --text-muted
--accent-blue, --accent-green, --accent-red, --accent-orange, --accent-yellow, --accent-purple
```

**Typography**: Geist for UI, Geist Mono for code (11px, line-height 1.2)

**Icons**: Lucide inline SVGs, 18x18 in 34x34 buttons, `stroke="currentColor"`

**Buttons**: `.btn-icon` — 34x34, `bg-card` + `border`, hover → `bg-hover`

**JS patterns**: `API.get(url)` wrapper (auto-handles 401), `Refresh.register()` for live data

**Responsive**: single 768px breakpoint, sidebar 220px above, hamburger below

## Design Decisions

### Query params vs path params for API

**Chose: query params** (`?path=/some/file`)

Path params would mean `/api/files/browse/home/user/file.txt` which creates issues:
- FastAPI path parameter parsing with dots in filenames
- URL encoding of slashes within the path component
- Ambiguity between API routes and file paths

Query params are simpler: `/api/files/browse?path=/home/user/file.txt`

### Single API call routing strategy

When navigating to `/files/home/user/something`, the JS doesn't know if it's a dir or file. Options:

1. **Try browse first, fall back to content** — two API calls in worst case
2. **Add `/api/files/info` endpoint** — one call to determine type, then fetch content
3. **Include type hint in URL** — ugly URLs like `/files/d/home/user/` vs `/files/f/home/user/file.py`

**Chose: try browse first.** If it returns an error (not a directory), treat as file. Simple, one API call for the common case (browsing dirs). For files, it's two calls but file loading is already async.

Actually, better approach: the browse endpoint can return `{type: "directory", ...}` or `{type: "file", info: ...}` depending on what the path is. Single endpoint, single call, always works.

**Final decision: `/api/files/browse` handles both.** If path is a directory, returns entries. If path is a file, returns file info (type, size, is_text, is_image). The JS then decides which view to show and fetches content/raw if needed.

### Text file size limit

2MB max for text content. Larger files show truncation notice + download link. This prevents memory blow-up on the backend and DOM issues on the frontend.

### Blocked filesystem paths

Block `/proc/`, `/sys/`, `/dev/` because:
- `/proc` entries can hang indefinitely when read
- `/sys` exposes kernel parameters
- `/dev` contains device nodes
- None of these are useful for a file browser

### Syntax highlighting approach

Reuse the exact pattern from commits module:
1. Render all `<code>` elements with textContent
2. Collect all text, join with newlines
3. `hljs.highlight()` or `hljs.highlightAuto()` the block
4. Split result by newlines, redistribute to individual `<code>` elements

This gives correct multi-line highlighting (string continuations, block comments).

### Why not reuse commits CSS classes?

Keep modules self-contained. The file table CSS is nearly identical to commits, but duplicating a few dozen lines of CSS is better than coupling two unrelated modules. If commits file view changes for commit-specific reasons, it won't affect the file browser.

## Key Files to Reference During Implementation

| File | What to reuse |
|------|---------------|
| `commits/routes.py` | Module structure, SPA page routes, path validation |
| `commits/static/commits.js` | SPA view switching, pushState routing, highlight.js integration, file table rendering |
| `commits/static/commits.css` | File table styles, back button, wrap toggle, mobile breakpoints |
| `commits/templates/commits.html` | Template structure, CDN includes, block layout |
| `notes/routes.py` | FileResponse pattern for serving raw files |
| `dashboard.py` | Router registration + static mount pattern |
| `templates/base.html` | Sidebar nav link format, icon convention |
| `docs/dashboard-architecture.md` | All CSS variables, spacing tokens, button patterns, typography |

## Open Questions

- Should hidden files (dotfiles) be shown? → Yes, but maybe dimmed
- Should we show file permissions? → Not in Phase 1, keep it simple
- What's the default starting directory? → `/` (root), user can bookmark favorites
- Should the breadcrumb show the full absolute path? → Yes, each segment clickable
