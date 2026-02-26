# Epic: Notes Editor

## Overview

Add a notes editor to the existing monitoring dashboard, giving direct browser access to Merlin's knowledge base and memory files. Edit from computer or phone, with git commit+push on save.

## Goals

1. **Manual access** — Read and edit KB notes, daily logs, and user.md without going through Merlin or SSH
2. **Mobile-friendly** — Works from phone browser (same auth as dashboard)
3. **Shareable links** — Merlin can send a URL in Discord that opens directly to a specific note
4. **Git sync** — Every save commits and pushes, with graceful fallback if push fails
5. **Integrated** — Part of the dashboard, not a separate tool. Same process, same auth, same port

## UX Design

### Navigation: Command Palette (not file tree)

No sidebar file tree. Instead, a **command palette** (like VS Code's Ctrl+P / Cmd+P):

- Triggered by a button in the header or keyboard shortcut (Ctrl+K or Ctrl+P)
- Fuzzy search (fuse.js) across all files in `memory/`
- Shows relative paths: `kb/zettelkasten-method`, `logs/2026-02-05`, `user`
- Results update as you type
- Enter or click opens the note
- Escape closes the palette

The palette is also the entry point for the Notes page — when you navigate to `/notes`, it opens automatically.

### Two Modes: View and Edit

**View mode** (`/notes/{path}`):
- Rendered markdown (HTML)
- YAML frontmatter parsed and displayed as a header: title, tags (as chips), created date, summary
- Images rendered inline
- Internal links (`[text](file.md)`) are clickable and navigate to the target note
- Syntax highlighting in code blocks (highlight.js or Prism)
- **Edit button** in the top-right corner

**Edit mode** (`/notes/{path}?edit=1`):
- Raw markdown in a code editor (CodeMirror or plain textarea)
- Full content including YAML frontmatter
- Drag & drop image/file upload (see Media section)
- **Save** and **Cancel** buttons
- Save writes to disk, commits, attempts push
- Cancel returns to view mode (discard changes)

### Media Upload (Drag & Drop)

In edit mode, drag & drop a file onto the editor:
- **Images** (png, jpg, gif, webp, svg): uploaded to `memory/media/`, markdown image syntax `![filename](media/filename.png)` inserted at cursor position
- **Other files**: uploaded to `memory/media/`, markdown link `[filename](media/filename.ext)` inserted
- Filenames are kept as-is (slugified if needed to avoid spaces/special chars)
- In view mode, images render inline, other files show as download links
- **Videos**: for V1, just a download link. Architecture allows adding preview later (YouTube embeds, HTML5 video)

### Shareable URLs

Every note has a clean, permanent URL:
```
https://<host>:3123/notes/kb/zettelkasten-method
https://<host>:3123/notes/logs/2026-02-05
https://<host>:3123/notes/user
```

Merlin can share these in Discord messages. Clicking opens the note in view mode (with auth).

### Index / Landing Page

`/notes` shows the command palette overlay on an index page:
- Quick stats: total notes, total KB entries, last modified note
- Recent notes (last 5 modified)
- Tag cloud or tag list with counts
- Palette opens on top of this

## Architecture

### Routes

| Route | Purpose |
|-------|---------|
| `GET /notes` | Index page + command palette |
| `GET /notes/{path:path}` | View note (rendered markdown) |
| `GET /notes/{path:path}?edit=1` | Edit note (raw markdown editor) |
| `GET /api/notes` | List all notes (for palette): `[{path, title, summary, tags, mtime}]` |
| `GET /api/notes/{path:path}` | Get note content (raw markdown string) |
| `PUT /api/notes/{path:path}` | Save note content → write + git commit + push |
| `POST /api/notes/upload` | Upload media file → save to `memory/media/`, return path |

### Git on Save

When `PUT /api/notes/{path}` is called:
1. Write content to `memory/{path}.md`
2. `git add memory/{path}.md`
3. `git commit -m "Update {path} via dashboard"`
4. Try `git push`:
   - Success → return `{"status": "saved", "pushed": true}`
   - Fail → return `{"status": "saved", "pushed": false, "push_error": "..."}`
5. Frontend shows green toast on success, yellow toast if push failed ("Saved & committed, push failed — will retry later")

### Frontend Libraries (CDN)

| Library | Purpose |
|---------|---------|
| [marked.js](https://marked.js.org/) | Markdown → HTML rendering |
| [highlight.js](https://highlightjs.org/) | Syntax highlighting in code blocks |
| [fuse.js](https://www.fusejs.io/) | Fuzzy search for command palette |
| [CodeMirror](https://codemirror.net/) (optional) | Syntax-highlighted editor (or plain textarea for V1) |

All loaded from CDN — no build step, consistent with existing dashboard approach.

### Code Structure

All notes editor code lives in `merlin-bot/notes/`, self-contained:

```
merlin-bot/
├── notes/
│   ├── __init__.py            # Exports `router` (FastAPI APIRouter)
│   ├── routes.py              # All routes: pages + API endpoints
│   ├── git_ops.py             # git add/commit/push helpers
│   ├── frontmatter.py         # YAML frontmatter parser
│   ├── templates/
│   │   ├── notes_index.html   # Index page + palette (extends base.html)
│   │   └── notes_view.html    # View + edit mode (extends base.html)
│   └── static/
│       ├── notes.css          # Notes-specific styles
│       └── notes.js           # Palette, editor, save, markdown rendering
```

Integration is one line in `dashboard.py`:
```python
from notes import router as notes_router
app.include_router(notes_router)
```

Templates extend the existing `base.html` (sidebar nav, auth, dark theme).
Static files are mounted at `/static/notes/`.

### File Discovery

`GET /api/notes` scans `memory/` recursively for `.md` files:
- Parses YAML frontmatter for title, summary, tags
- Returns mtime for sorting
- Excludes `.state.json`, `.history.json`, `digest-history.json` (non-note files)
- Includes: `kb/*.md`, `logs/*.md`, `user.md`, `app-ideas-history.md`, `self-reflection-history.md`

### Link Resolution

In rendered markdown view, internal links are rewritten:
- `[text](zettelkasten-ai-assistant.md)` → `<a href="/notes/kb/zettelkasten-ai-assistant">text</a>`
- Links within `kb/` resolve relative to `kb/`
- Links with explicit paths resolve as-is
- External links (http/https) open in new tab

### Media Directory

```
memory/
├── media/               # Uploaded files (images, documents)
│   ├── photo.png
│   └── diagram.svg
├── kb/
├── logs/
└── ...
```

`memory/media/` is gittracked (binary files). For V1, no size limits — revisit if needed.

## Integration with Existing Dashboard

- **Sidebar nav**: Add "Notes" link after "Logs"
- **Same auth**: HTTP Basic Auth (DASHBOARD_USER / DASHBOARD_PASS)
- **Same CSS theme**: Use existing dark theme variables from `dashboard.css`
- **Same process**: Routes added to the existing FastAPI app in `dashboard.py`

## Mobile Considerations

- Command palette: full-width on mobile, touch-friendly hit targets
- View mode: content fills screen, images scale to fit
- Edit mode: full-width textarea, Save/Cancel buttons sticky at bottom
- Drag & drop may not work on mobile — add a file input button as fallback

## V1 Scope

### In Scope
- [x] Command palette with fuzzy search
- [x] View mode with rendered markdown, images, clickable links, syntax highlighting
- [x] Edit mode with raw markdown editor
- [x] Save with git commit + push (graceful push failure)
- [x] Drag & drop image upload to `memory/media/`
- [x] Shareable URLs per note
- [x] Index page with recent notes and tag cloud
- [x] YAML frontmatter rendered as header in view mode
- [x] Mobile responsive
- [x] Integrated in dashboard sidebar

### Out of Scope (future)
- [x] Create new note from dashboard — via command palette, "+ Create {path}" option
- [x] Delete notes from dashboard — Delete button with confirm(), git rm + commit + push
- [ ] Real-time / auto-save (Notion-style)
- [ ] WYSIWYG editor
- [ ] Video preview / YouTube embeds
- [ ] Full-text search (use Merlin's `memory_search.py`)
- [ ] Git history / diff viewer
- [ ] Collaborative editing

## Acceptance Criteria

### Must Have
- [ ] `/notes` page accessible from dashboard sidebar
- [ ] Command palette lists all memory files with fuzzy search
- [ ] Clicking a note opens rendered markdown view
- [ ] Internal links navigate to the correct note
- [ ] Images render inline in view mode
- [ ] Code blocks have syntax highlighting
- [ ] Edit button switches to raw markdown editor
- [ ] Save writes file, git commits, attempts push
- [ ] Push failure shows warning (does not block save)
- [ ] Drag & drop image uploads to `memory/media/`
- [ ] Uploaded images appear in markdown via inserted syntax
- [ ] Shareable URLs work (e.g., `/notes/kb/zettelkasten-method`)
- [ ] Mobile responsive (palette, view, edit all usable on phone)
- [ ] Same auth as rest of dashboard

### Should Have
- [ ] YAML frontmatter rendered as styled header (title, tags, date)
- [ ] Index page with recent notes and tag stats
- [ ] Toast notifications for save success/failure
- [ ] Keyboard shortcut to open palette (Ctrl+K)

### Nice to Have
- [ ] CodeMirror editor with markdown syntax highlighting
- [ ] File upload button (fallback for mobile drag & drop)
- [ ] Note creation from palette (type a new name → create blank note)

## Decisions Made

1. **Command palette over file tree** — Cleaner, works better on mobile, suits flat structure
2. **Standard markdown links** — Keep `[text](file.md)` format, no `[[wikilinks]]`. Universal compatibility.
3. **Explicit Edit/Save** — No auto-save. Save triggers git commit. Simple and predictable.
4. **Media in `memory/media/`** — Colocated with notes, tracked in git
5. **CDN libraries** — No build step, consistent with dashboard
6. **Single template for view+edit** — JS toggles between modes, avoids page reload
7. **No new/delete in V1** — Creation goes through Merlin (preserves auto-linking). Deletion via git/CLI.

## References

- [Dashboard architecture](../../docs/dashboard-architecture.md) — Existing theme, CSS conventions, JS patterns
- [Memory system epic](../archive/memory-system/requirements.md) — KB format, Zettelkasten philosophy
- [Flatnotes](https://github.com/dullage/flatnotes) — UX inspiration
- [SilverBullet](https://silverbullet.md/) — Feature inspiration
- [marked.js](https://marked.js.org/) — Markdown renderer
- [fuse.js](https://www.fusejs.io/) — Fuzzy search
