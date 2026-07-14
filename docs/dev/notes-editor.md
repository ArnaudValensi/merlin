# Notes Editor

Reference documentation for the web-based markdown notes editor integrated into the dashboard.

## Overview

The notes editor provides a browser-based interface for viewing and editing Merlin's notes files (`notes/`). It features a command palette with fuzzy search, rendered markdown view, CodeMirror editor with vim mode, and background git sync.

## Architecture

```
notes/
├── __init__.py          # Package init
├── routes.py            # FastAPI routes + API endpoints
├── sync.py              # Background git sync (auto-commit/push/pull)
├── frontmatter.py       # YAML frontmatter parser
├── templates/
│   ├── notes_index.html # Index page + command palette
│   ├── notes_view.html  # View + edit mode
│   └── notes_tag.html   # Tag filter page
└── static/
    ├── notes.css        # Notes-specific styles
    └── notes.js         # Palette, editor, save, markdown rendering
```

Notes is a built-in extension: it exports `api_router` + `page_router` and is
mounted by the extension loader through `mount_module` (slug `notes`), so its
routes live at `/api/notes/*` and `/notes*`. Static files at `/static/notes/`.
Templates extend `base.html`.

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
| `/api/notes` | GET | List all notes files (path, title, summary, tags, mtime) |
| `/api/notes/{path}` | GET | Read raw markdown content |
| `/api/notes/{path}` | PUT | Save file to disk (sync watcher handles commit/push) |
| `/api/notes/{path}` | DELETE | Delete file from disk (sync watcher handles commit/push) |
| `/api/notes/upload` | POST | Upload media to `notes/media/` |
| `/api/notes/search?q=` | GET | Full-text fuzzy search via fzf |
| `/api/notes/sync-status` | GET | Returns list of conflicted files |

### File Discovery (`GET /api/notes`)

Scans `notes/` recursively for `.md` files:
- Parses YAML frontmatter for title, summary, tags
- Returns mtime for sorting
- Excludes non-note files (`.history.json`, `digest-history.json`)

### Content Search (`GET /api/notes/search?q=`)

Server-side fuzzy search via fzf:
- Builds a search index of all `.md` lines (`path\tlinenum\tcontent`)
- Pipes through `fzf --filter` with `--nth=3` (searches content only)
- Returns ranked matches with path, line number, and surrounding context
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

- Typing `/query` switches to server-side fzf search
- Debounced at 300ms
- Results show: path, line number, matching line with highlighted term
- Loading indicator while searching

### Navigation

- Arrow keys: up/down through results
- Enter: open selected note
- Escape: close palette

## Edit Mode

- Toggle between view and edit via Edit button
- CodeMirror editor with `material-darker` theme
- Vim keybindings by default (`jj` mapped to Escape), toggleable VIM/STD button
- Save: writes file to disk, switches back to view mode (sync watcher commits/pushes in background)
- Toast notifications: green (saved), red (error)

## Media Upload

- Drag & drop on CodeMirror editor
- File input button (mobile fallback)
- Uploads to `notes/media/`
- Returns relative path
- Inserts markdown image/link syntax at cursor position

## Git Sync (`sync.py`)

Background sync that automatically commits and pushes notes changes, and periodically pulls from remote to sync across devices (e.g., Obsidian on another machine).

All file operations (save, delete, upload) just write to disk — the sync watcher detects changes and handles all git operations.

### Setup

1. **Create a GitHub repo** (public or private)

2. **Generate a fine-grained personal access token** on GitHub:
   - Settings → Developer settings → Personal access tokens → Fine-grained tokens
   - Select the repo, give it **Contents** read/write permission

3. **Store the token in git credential store**:
   ```bash
   git config --global credential.helper store
   echo "https://<username>:<token>@github.com" >> ~/.git-credentials
   ```

4. **In Merlin Settings**, enable **Git Sync** and set the remote URL:
   ```
   https://github.com/<username>/<repo>.git
   ```

5. **Restart Merlin** — sync starts automatically

### How It Works

Two background tasks run concurrently:

- **Debounced watcher**: detects uncommitted changes, waits for the debounce period (default 20s), then commits all changes with message "sync notes" and pushes to remote
- **Periodic puller**: pulls from remote at a regular interval (default 60s) to catch changes from other devices

### Settings

| Setting | Env Var | Default | Description |
|---------|---------|---------|-------------|
| Git Sync | `NOTES_GIT_SYNC` | off | Enable/disable background sync |
| Git Remote | `NOTES_GIT_REMOTE` | none | Remote repository URL |
| Debounce | `NOTES_SYNC_DEBOUNCE` | 20s | Delay before committing after a change |
| Pull Interval | `NOTES_SYNC_PULL_INTERVAL` | 60s | How often to pull from remote |

### Merge Conflicts

When a pull results in merge conflicts:
- Conflict markers (`<<<<<<<` / `>>>>>>>`) are preserved in the file
- The conflicted state is committed to avoid a stuck repo
- Conflicted files are tracked and exposed via `GET /api/notes/sync-status`
- Conflicts resolve automatically once the markers are edited out of the file

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
- **CodeMirror** — Editor (markdown mode, vim keymap, material-darker theme)

## Key Files

| File | Purpose |
|------|---------|
| `notes/routes.py` | API endpoints and page routes |
| `notes/sync.py` | Background git sync (auto-commit, push, pull, conflict detection) |
| `notes/frontmatter.py` | YAML frontmatter parsing |
| `notes/static/notes.js` | Palette, editor, sync status, markdown rendering |
| `notes/static/notes.css` | Notes-specific styles |
| `notes/templates/notes_index.html` | Index page |
| `notes/templates/notes_view.html` | View/edit page |
| `notes/templates/notes_tag.html` | Tag filter page |
