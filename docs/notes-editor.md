# Notes Editor

Reference documentation for the web-based markdown notes editor integrated into the dashboard.

## Overview

The notes editor provides a browser-based interface for viewing and editing Merlin's memory files (`memory/`). It features a command palette with fuzzy search, rendered markdown view, raw editor, and git-backed saving.

## Architecture

```
notes/
├── __init__.py          # Package init
├── routes.py            # FastAPI routes + API endpoints
├── git_ops.py           # Git add/commit/push operations
├── frontmatter.py       # YAML frontmatter parser
├── templates/
│   ├── notes_index.html # Index page + command palette
│   ├── notes_view.html  # View + edit mode
│   └── notes_tag.html   # Tag filter page
└── static/
    ├── notes.css        # Notes-specific styles
    └── notes.js         # Palette, editor, save, markdown rendering
```

Mounted in `main.py`:
```python
from notes import router as notes_router
app.include_router(notes_router)
```

Static files at `/static/notes/`. Templates extend `base.html`.

## Pages & Routes

### Index (`/notes`)

Landing page with:
- Recent notes (sorted by mtime)
- Tag cloud with counts
- Stats (total notes, total tags)
- Command palette (Ctrl+K)

### Note View (`/notes/{path}`)

Single note page with:
- Rendered markdown (via marked.js)
- Syntax highlighting (highlight.js, github-dark-dimmed theme)
- YAML frontmatter rendered as styled header (title, tags, date)
- Internal links rewritten to `/notes/{path}`
- Edit/Delete buttons in toolbar
- Shareable URL

### Tag Page (`/notes/tag/{tag}`)

Lists all notes with a specific tag. Sortable by recent, name, or connections.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/notes` | GET | List all memory files (path, title, summary, tags, mtime) |
| `/api/notes/{path}` | GET | Read raw markdown content |
| `/api/notes/{path}` | PUT | Save file + git commit + push |
| `/api/notes/{path}` | DELETE | Delete file + git rm + commit + push |
| `/api/notes/upload` | POST | Upload media to `memory/media/` |
| `/api/notes/search?q=` | GET | Full-text content search |

### File Discovery (`GET /api/notes`)

Scans `memory/` recursively for `.md` files:
- Parses YAML frontmatter for title, summary, tags
- Returns mtime for sorting
- Excludes non-note files (`.state.json`, `.history.json`, `digest-history.json`)

### Content Search (`GET /api/notes/search?q=`)

Server-side case-insensitive substring search:
- Scans all `.md` files in `memory/`
- Returns matching lines with path, line number, context
- Max 50 results
- Used by command palette with `/` prefix

## Command Palette

Opened via Ctrl+K from any notes page.

### File Search Mode (default)

- Client-side fuzzy search via fuse.js
- Searches: path, title, summary, tags
- Results show: path, summary, tag chips
- `+ Create {path}` option for non-existent paths

### Content Search Mode (`/` prefix)

- Typing `/query` switches to server-side content search
- Debounced at 300ms
- Results show: path, line number, matching line with highlighted term
- Loading indicator while searching

### Navigation

- Arrow keys: up/down through results
- Enter: open selected note
- Escape: close palette

## Edit Mode

- Toggle between view and edit via Edit button
- Raw markdown textarea (CodeMirror not used — plain textarea)
- Font size 16px on mobile (prevents iOS auto-zoom)
- Save: writes file → git add → git commit → git push
- Push failures show yellow warning (save still succeeds locally)
- Toast notifications: green (success), yellow (push failed), red (error)

## Media Upload

- Drag & drop on editor textarea
- File input button (mobile fallback)
- Uploads to `memory/media/`
- Returns relative path
- Inserts markdown image syntax: `![filename](media/filename.png)`

## Git Operations (`git_ops.py`)

### Save Flow

```
PUT /api/notes/{path}
  → Write file to disk
  → git add {path}
  → git commit -m "Update {path}"
  → git push (async, non-blocking)
     ├─ Success → done
     └─ Failure → log warning, save still succeeds
```

### Delete Flow

```
DELETE /api/notes/{path}
  → git rm {path}
  → git commit -m "Delete {path}"
  → git push
```

### Create Flow

```
PUT /api/notes/{new_path} (file doesn't exist yet)
  → Write file with frontmatter template
  → git add {path}
  → git commit -m "Create {path}"
  → git push
```

## Internal Link Resolution

In rendered markdown, links are rewritten:
- `[text](other-note.md)` → `<a href="/notes/kb/other-note">text</a>`
- Links within `kb/` resolve relative to `kb/`
- External links (http/https) open in new tab

## Frontmatter Parser (`frontmatter.py`)

Extracts YAML between `---` delimiters:

```python
def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Returns (metadata_dict, body_without_frontmatter)"""
```

Fields extracted: `title`, `created`, `tags`, `related`, `summary`.

## CDN Dependencies

No build step — all via CDN:
- **marked.js** — Markdown rendering
- **highlight.js** — Syntax highlighting (github-dark-dimmed theme)
- **fuse.js** — Client-side fuzzy search

## Key Files

| File | Purpose |
|------|---------|
| `notes/routes.py` | API endpoints and page routes |
| `notes/git_ops.py` | Git add/commit/push |
| `notes/frontmatter.py` | YAML frontmatter parsing |
| `notes/static/notes.js` | Palette, editor, markdown rendering |
| `notes/static/notes.css` | Notes-specific styles |
| `notes/templates/notes_index.html` | Index page |
| `notes/templates/notes_view.html` | View/edit page |
| `notes/templates/notes_tag.html` | Tag filter page |
