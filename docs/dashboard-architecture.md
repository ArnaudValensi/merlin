# Dashboard Architecture & UI Conventions

Reference for maintaining and extending the Merlin monitoring dashboard.

## Stack

| Layer | Tech | Notes |
|-------|------|-------|
| Backend | FastAPI | Async, same event loop as Discord bot |
| Templates | Jinja2 | Server-side rendered, extends `base.html` |
| Charts | Chart.js 4 + chartjs-adapter-date-fns + chartjs-plugin-zoom | CDN |
| Syntax highlighting | highlight.js 11 | CDN, `github-dark-dimmed` theme |
| Auth | Cookie-based (HMAC-signed) | See [`auth-and-tunnel.md`](auth-and-tunnel.md) |
| Data | JSONL flat file | `logs/structured.jsonl`, append-only |

## File Layout

```
merlin/
├── main.py                    # Entry point: FastAPI app, auth, module registration, tunnel
├── auth.py                    # Cookie-based auth (HMAC-signed session cookies)
├── tunnel.py                  # Cloudflare Tunnel manager
├── templates/
│   ├── base.html              # Shared layout: dynamic sidebar, nav, CDN scripts
│   └── login.html             # Password login page
├── static/
│   ├── dashboard.css          # Dark theme, responsive
│   └── dashboard.js           # Shared JS: API, auto-refresh, formatting
├── files/                     # File browser module
├── terminal/                  # Web terminal module
├── commits/                   # Commit browser module
├── notes/                     # Notes editor module
└── merlin-bot/
    ├── merlin_app.py          # App plugin: monitoring pages (overview, performance, logs)
    ├── structured_log.py      # JSONL writer (thread-safe, used by all emitters)
    └── templates/             # Bot-specific templates (overview, performance, logs, session)
```

## Architecture Decisions

### Server-side rendered pages, client-side data

Pages are Jinja2 templates served by FastAPI. Data is fetched client-side via JSON API endpoints. This gives us:
- Fast initial page load (HTML is lightweight, no SPA framework)
- Dynamic data refresh without full page reload
- Easy to add new pages (just a template + route)

### Single JSONL log file

All events go to one file (`logs/structured.jsonl`). Each line is a JSON object with a `type` field. Types:
- `invocation` — Claude Code call (fields: caller, prompt, duration, exit_code, num_turns, tokens_in, tokens_out, session_id, model)
- `bot_event` — Discord bot events (fields: event, details, content for message_received)
- `cron_dispatch` — Cron job lifecycle (fields: job_id, event, duration, exit_code)

### Auto-refresh via mtime polling

JS polls `/api/last-modified` every 5 seconds. The endpoint returns the mtime of `structured.jsonl`. When mtime changes, registered callbacks re-fetch data. This avoids unnecessary API calls when nothing has changed.

```
Refresh.register(myLoadFunction);  // register callback
Refresh.start(5000);               // start polling
```

### Dashboard is the entry point

`main.py` starts uvicorn + optional Cloudflare tunnel + Discord bot + cron scheduler in a single process. Port 3123 by default.

Start command: `uv run main.py` (or `restart.sh` to restart in background).

## CSS Conventions

### Theme

Dark theme using CSS custom properties in `:root`:

```css
--bg-primary: #0f1117      /* page background */
--bg-secondary: #1a1d27    /* sidebar, detail sections */
--bg-card: #222633          /* cards, chart containers */
--bg-hover: #2a2e3d         /* row hover */
--border: #2e3347           /* borders, grid lines */
--text-primary: #e4e6ed     /* headings, main text */
--text-secondary: #8b8fa3   /* body text, labels */
--text-muted: #5c6078       /* timestamps, less important */
--accent-blue: #4a9eff      /* links, active states */
--accent-green: #34d399     /* success, online */
--accent-red: #f87171       /* errors */
--accent-orange: #fb923c    /* cron badge */
--accent-yellow: #fbbf24    /* warnings */
--accent-purple: #a78bfa    /* invocation badge */
```

Always use these variables — never hardcode colors.

### Responsive Breakpoint

Single breakpoint at `768px`:
- Above: sidebar visible (220px), main content offset
- Below: sidebar hidden, hamburger button shows, overlay on tap

### Component Classes

| Class | Usage |
|-------|-------|
| `.card` | Container with bg-card, border, border-radius |
| `.card-grid` | CSS grid for status cards (auto-fill, minmax 200px) |
| `.feed-item` | Row in activity feed |
| `.feed-badge` | Colored label (`.badge-invocation`, `.badge-bot_event`, `.badge-cron_dispatch`, `.badge-error`, `.badge-success`) |
| `.log-table` | Full-width table for logs |
| `.row-detail` | Hidden expandable row (toggle `.open` to show) |
| `.detail-section` | Styled block inside expandable row (blue left border) |
| `.tabs` / `.tab` | Tab bar with `.active` state |
| `.filters` | Flexbox row for filter controls |
| `.empty-state` | Centered icon + message when no data |
| `.chart-container` | Wrapper for Chart.js canvases |
| `.time-range` | Button group for time range selector |

## Design System

### Principles

- **Dark-first** — All components designed for the dark theme. Never hardcode colors; always use CSS variables.
- **Bordered containers** — Interactive elements (buttons, cards, inputs) have `bg-card` background + `border`. This makes them feel grounded and clickable.
- **Subtle until hovered** — Default state is muted (`text-secondary` or `text-muted`). Hover brightens to `text-primary` or shifts to accent color.
- **No build step** — All assets are plain CSS/JS or CDN. No preprocessors, bundlers, or frameworks.
- **Single breakpoint** — `768px` separates desktop from mobile. One breakpoint keeps responsive logic simple.

### Icons

Source: [Lucide](https://lucide.dev/) (MIT license, successor to Feather Icons).

Convention: inline `<svg>` elements — no icon font, no sprite sheet, no library to load.

```html
<svg width="18" height="18" viewBox="0 0 24 24" fill="none"
     stroke="currentColor" stroke-width="2"
     stroke-linecap="round" stroke-linejoin="round">
  <path d="..."/>
</svg>
```

Standard size is `18x18` inside a `34x34` button. Icons inherit color from the parent via `stroke="currentColor"`.

### Buttons

**Icon buttons** (`.btn-icon`) — compact square buttons for toolbars:

| Modifier | Idle | Hover | Use case |
|----------|------|-------|----------|
| *(none)* | `bg-card`, `text-secondary` | `bg-hover`, `text-primary` | Default actions (edit, cancel) |
| `.btn-icon-primary` | `accent-blue`, white | Darker blue | Primary action (save) |
| `.btn-icon-danger` | `bg-card`, `text-secondary` | Red border + `accent-red` | Destructive action (delete) |

```css
.btn-icon {
    width: 34px; height: 34px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg-card);
}
```

**Text buttons** — used when icons aren't enough (e.g., vim mode toggle `.btn-vim`). Same `bg-card` + `border` base, monospace font for mode indicators.

**Trigger buttons** (`.palette-trigger`) — wider, with label text + keyboard hint. Used for search / command palette entry points.

### Spacing

| Token | Value | Usage |
|-------|-------|-------|
| Tight | `4px` | Between related icon buttons in a toolbar group |
| Normal | `8px` | Between unrelated toolbar items, form fields |
| Section | `16px` | Between content sections, toolbar-to-content |
| Card | `20px` | Padding inside cards, grid gaps |
| Page | `24px–28px` | Main content padding |

### Typography

Primary font: **Geist** by Vercel (loaded from Google Fonts in `base.html`).

| Context | Font | Size | Weight |
|---------|------|------|--------|
| Body text | Geist | 14–16px | 400 |
| Headings | Geist | 16–28px | 600–700 |
| Notes editor (CodeMirror) | Geist | 15px | 400 |
| Notes rendered view | Geist | 16px | 400 |
| Code blocks / logs | `'Geist Mono', monospace` | 11px | 400 |
| Badges / labels | Geist | 11–12px | 500–600 |
| Mode indicators (VIM) | `'Geist Mono', monospace` | 11px | 700 |

Geist is used for all UI text. Geist Mono (loaded from Google Fonts alongside Geist) is used for code blocks, log entries, terminals, and mode indicators.

The notes editor uses Geist at `15px` with `line-height: 1.75`, matching the rendered view for a seamless writing experience.

### Color Usage

| Color | Semantic meaning |
|-------|-----------------|
| `accent-blue` | Primary actions, links, active nav, save |
| `accent-green` | Success, online status, vim mode active |
| `accent-red` | Errors, destructive actions (delete hover) |
| `accent-yellow` | Warnings (push failed) |
| `accent-orange` | Cron-related badges |
| `accent-purple` | Invocation badges |

For tinted backgrounds (active states, hover), use the accent color at 8–15% opacity:
```css
background: rgba(52, 211, 153, 0.08);   /* green tint */
border-color: rgba(52, 211, 153, 0.3);  /* green border */
```

### Toast Notifications

Toasts appear bottom-right, auto-dismiss after 4 seconds. Three variants:

| Type | Background | Border | Text color |
|------|-----------|--------|------------|
| `.toast-success` | Green 15% | Green 30% | `accent-green` |
| `.toast-warning` | Yellow 15% | Yellow 30% | `accent-yellow` |
| `.toast-error` | Red 15% | Red 30% | `accent-red` |

### Toolbar Pattern

Toolbars use flexbox with `justify-content: space-between`:
- **Left side**: navigation/search (palette trigger)
- **Right side**: action buttons (edit, save, cancel, delete)

On mobile, the right side wraps with `flex-wrap: wrap` and `justify-content: flex-end`.

Buttons that only appear in certain modes (edit vs view) are toggled via inline `style.display` from JS, not CSS classes.

## JS Conventions

### Modules in `dashboard.js`

- **`API.get(url)`** — fetch wrapper, auto-handles 401 (reloads page for re-auth)
- **`Refresh`** — mtime-based auto-refresh. Call `.register(cb)` then `.start(ms)`
- **Time formatting** — `formatTime`, `formatDateTime`, `timeAgo`, `formatDuration`, `utcString` — all render in browser local timezone, UTC on hover via `title` attribute
- **`typeBadge(type)`** — returns badge HTML for event type
- **`statusBadge(event)`** — returns OK/Error badge
- **`eventSummary(event)`** — one-line summary string per event type
- **`updateBotStatus()`** — green/red dot in sidebar header
- **`configureChartDefaults()`** — sets Chart.js colors for dark theme
- **`navigateTo(page, params)`** — cross-page linking with query params

### Per-page JS

Each template has a `{% block scripts %}` with page-specific logic. Pattern:

```javascript
document.addEventListener('DOMContentLoaded', () => {
    loadData();                    // initial fetch
    Refresh.register(loadData);    // re-fetch on data change
    Refresh.start(5000);           // poll every 5s
});
```

### Chart.js Patterns

- Call `configureChartDefaults()` before creating charts
- Use `type: 'time'` axis with `chartjs-adapter-date-fns` for time series
- Scatter for execution time over time (color by caller type)
- Bar for per-job breakdown (grouped: avg + p95)
- Doughnut for success rate
- Enable zoom plugin for scatter: `zoom: { zoom: { wheel: { enabled: true } }, pan: { enabled: true } }`

## API Endpoints

### Core (main.py + merlin_app.py)

| Endpoint | Returns |
|----------|---------|
| `GET /api/health` | `{ status, bot_start_time, tunnel_url, ... }` |
| `GET /api/events?type=&since=&until=&status=` | Array of all events, filtered |
| `GET /api/invocations?since=&until=&caller=` | Invocation events only |
| `GET /api/jobs` | Per-job stats with recent run history |
| `GET /api/last-modified` | `{ mtime }` of structured.jsonl |
| `GET /api/session/{filename}` | Session JSONL events as JSON array |

### Notes

| Endpoint | Returns |
|----------|---------|
| `GET /api/notes` | List all notes: `[{path, title, summary, tags, mtime}]` |
| `GET /api/notes/{path}` | Raw markdown content of a note |
| `PUT /api/notes/{path}` | Save note, git commit+push |
| `POST /api/notes/upload` | Upload media file to `memory/media/` |

### Commits

| Endpoint | Returns |
|----------|---------|
| `GET /api/commits?skip=&limit=&search=&since=&until=` | Paginated commit list with stats |
| `GET /api/commits/{hash}` | Single commit metadata + file list |
| `GET /api/commits/{hash}/diff` | Parsed unified diff (hunks + lines) |
| `GET /api/commits/{hash}/file/{path}` | Full file content with gutter annotations |

### Files

| Endpoint | Returns |
|----------|---------|
| `GET /api/files/browse?path=` | Directory listing OR file info (auto-detects) |
| `GET /api/files/content?path=` | Text file content (up to 2 MB, with truncation flag) |
| `GET /api/files/raw?path=` | Raw file (FileResponse for images/downloads) |

## Modules

All modules follow the same self-contained structure:

```
module/
├── __init__.py       # Exports router + STATIC_DIR
├── routes.py         # APIRouter with page routes + API endpoints
├── templates/        # Jinja2 templates extending base.html
└── static/           # Module-specific CSS + JS
```

Registration in `main.py`:
```python
from module import router as module_router, MODULE_STATIC_DIR
app.include_router(module_router, dependencies=[Depends(require_auth)])
app.mount("/static/module", StaticFiles(directory=str(MODULE_STATIC_DIR)), name="module-static")
```

Static mounts go **before** the general `/static` mount for route priority.

### Notes Editor

```
notes/
├── __init__.py            # Exports router + NOTES_STATIC_DIR
├── routes.py              # Pages (/notes, /notes/{path}) + API endpoints
├── git_ops.py             # Async git add/commit/push
├── frontmatter.py         # YAML frontmatter parser
├── templates/
│   ├── notes_index.html   # Index: recent notes, tag cloud, command palette
│   └── notes_view.html    # View + edit mode (JS toggles between them)
└── static/
    ├── notes.css          # Notes-specific dark theme styles
    └── notes.js           # Palette (fuse.js), viewer (marked.js), editor, upload
```

**Pages:** `/notes` (index), `/notes/{path}` (view/edit), `/notes/tags/{tag}` (tag filter)

**CDN deps:** fuse.js (fuzzy search), marked.js (markdown rendering), highlight.js (code blocks).

### Commit Browser

```
commits/
├── __init__.py            # Exports router + COMMITS_STATIC_DIR
├── routes.py              # Pages + API endpoints
├── git_parser.py          # Git log/diff/show parsing (subprocess calls to git)
├── templates/
│   └── commits.html       # SPA: 3 views (list, diff, file)
└── static/
    ├── commits.css        # Diff colors, file table, gutter FAB, mobile
    └── commits.js         # IIFE: routing, diff rendering, highlight.js, gutter nav
```

**Pages:** `/commits` (list), `/commits/{hash}` (diff), `/commits/{hash}/file/{path}` (full file)

**SPA pattern:** Single template with 3 views toggled via `display: none`. Uses `history.pushState` + `popstate` for browser navigation. `routeFromUrl()` on load for deep linking.

**Syntax highlighting:** Collects all `<code>` elements, joins text, runs `hljs.highlight()` as a single block, then splits result by newlines and redistributes to individual elements. This preserves correct multi-line highlighting (string continuations, block comments).

**Gutter navigation:** File view has colored gutter bars (green=added, blue=modified, red=deleted) with a floating action button (FAB) for prev/next change navigation. Diff mode toggle shows deleted lines inline.

**CDN deps:** highlight.js (`github-dark-dimmed` theme).

### File Browser

```
files/
├── __init__.py            # Exports router + FILES_STATIC_DIR
├── routes.py              # Pages + API endpoints
├── fs_helpers.py          # Path validation, directory listing, file reading, type detection
├── templates/
│   └── files.html         # SPA: 2 views (directory listing, file viewer)
└── static/
    ├── files.css          # Directory entries, breadcrumbs, image preview, binary info, mobile
    └── files.js           # IIFE: routing, browse, text/image/binary rendering
```

**Pages:** `/files` (root `/`), `/files/{path}` (deep link to any filesystem path)

**SPA pattern:** Single template with 2 views (directory listing + file viewer). The `/api/files/browse` endpoint returns `{type: "directory", ...}` or `{type: "file", ...}` — JS decides which view to show based on the response.

**API uses query params** (`?path=/some/file`) instead of path params to avoid URL encoding issues with slashes and dots in filenames.

**Security:** All paths resolved via `validate_path()` which resolves symlinks/`..`, then blocks `/proc/`, `/sys/`, `/dev/`. Read-only (no write endpoints).

**File type detection:** Multi-strategy — file extension, known filenames (e.g. `Makefile`), MIME type, and binary sniffing (null byte detection) for extensionless files.

**Text files:** Rendered with line numbers + highlight.js syntax highlighting. 2 MB size limit with truncation notice + download link. Wrap toggle for long lines.

**Images:** Inline preview via `/api/files/raw`, with size + MIME info.

**Binary files:** Info card with file icon, name, size, extension, and download button.

**CDN deps:** highlight.js (`github-dark-dimmed` theme).

### Web Terminal

```
terminal/
├── __init__.py            # Exports router
├── routes.py              # Page route + WebSocket endpoint + voice transcription
└── templates/
    └── terminal.html      # xterm.js terminal, mobile toolbar, voice input
```

**Pages:** `/terminal`

**WebSocket:** `/ws/terminal` — bridges browser to a PTY/tmux session. Auth via session cookie on the WebSocket upgrade request (browsers send cookies automatically).

**Features:** xterm.js terminal emulator, tmux session persistence (`merlin-dev`), mobile touch toolbar, voice input via Whisper transcription.

**CDN deps:** xterm.js, xterm-addon-fit, xterm-addon-web-links.

**Note:** Terminal handles its own auth internally (WebSocket cookie check) rather than using `Depends(require_auth)` on the router.

## Adding a New Page

1. Create `templates/newpage.html` extending `base.html`
2. Add route in the appropriate module's `routes.py` (or `main.py` for core pages)
3. Add nav item to `CORE_NAV_ITEMS` in `main.py` (or `MERLIN_APP_NAV_ITEMS` in `merlin_app.py` for bot pages)
4. Use `Refresh.register()` + `Refresh.start()` for live data
5. Validate with screenshots: `uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --user admin --pass <pass>`

## Structured Log Format

### Writing events

```python
from structured_log import log_event

log_event("invocation", caller="discord", prompt="...", duration=5.2, exit_code=0, ...)
log_event("bot_event", event="message_received", details="...", content="...")
log_event("cron_dispatch", job_id="daily-digest", event="completed", duration=45.0, ...)
```

### Reading events

`merlin_app.py` has `read_events(event_type=None, since=None, until=None)` which parses the JSONL with optional filtering.

## Visual Validation

Always validate UI changes with screenshots before marking done:

```bash
uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --user admin --pass <pass>
```

This captures 18 screenshots (3 pages x 6 viewports). Read the PNGs to verify layout, responsiveness, and rendering.
