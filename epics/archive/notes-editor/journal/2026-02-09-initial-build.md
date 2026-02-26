# 2026-02-09 — Notes Editor: Initial Build

## What Was Done

Built and shipped a complete notes editor integrated into the Merlin dashboard. All 25 tasks across 5 phases completed in one session.

## Context & Motivation

The user wanted manual access to Merlin's knowledge base (memory/kb/, logs, user.md) from both computer and phone, without needing to go through Merlin or SSH. We explored several options:

- **Obsidian + obsidian-git** — best standalone option, but requires separate app
- **SilverBullet** — powerful self-hosted wiki, but heavy for the need
- **Flatnotes** — simple self-hosted editor, nice aesthetic, but Docker-only
- **Ghost/Koenig** — rejected (CMS, not a note/wiki tool)
- **Built into dashboard** (chosen) — zero new dependencies, same process/auth/port, fully integrated

The decision was to build a simple markdown editor into the existing dashboard rather than adding an external tool. The KB is already just markdown files — we only need a way to view, edit, and navigate them.

## Architecture

Self-contained module in `merlin-bot/notes/`:

```
notes/
├── __init__.py            # Exports router + NOTES_STATIC_DIR
├── routes.py              # Pages + API endpoints (7 routes)
├── git_ops.py             # Async git add/commit/push
├── frontmatter.py         # YAML frontmatter parser
├── templates/
│   ├── notes_index.html   # Index: recent notes, tag cloud, palette
│   └── notes_view.html    # View + edit (JS toggles between modes)
└── static/
    ├── notes.css          # Dark theme styles (~350 lines)
    └── notes.js           # Palette, viewer, editor, upload (~400 lines)
```

Mounted in dashboard.py with one line: `app.include_router(notes_router)`.

### Key Design Decisions

1. **Command palette over file tree** — VS Code-style Ctrl+K fuzzy search. Cleaner than a sidebar, works better on mobile, suits the flat file structure.
2. **Standard markdown links** — kept `[text](file.md)` format, no `[[wikilinks]]`. Universal compatibility with Obsidian, GitHub, etc.
3. **Explicit Edit/Save** — not auto-save. Save triggers git commit+push. Simple and predictable.
4. **CDN libraries** — fuse.js, marked.js, highlight.js. No build step, consistent with dashboard approach.
5. **Media in `memory/media/`** — uploaded files colocated with notes, tracked in git.

### API Endpoints

| Route | Purpose |
|-------|---------|
| `GET /notes` | Index page |
| `GET /notes/{path}` | View note (also serves media files) |
| `GET /api/notes` | List all notes with metadata |
| `GET /api/notes/{path}` | Read raw markdown content |
| `PUT /api/notes/{path}` | Save + git commit + push |
| `POST /api/notes/upload` | Upload file to memory/media/ |

### Frontend Features

- **Command palette**: fuse.js fuzzy search across path, title, summary, tags. Keyboard nav (up/down/enter/escape). Ctrl+K shortcut.
- **View mode**: marked.js rendering, highlight.js code blocks (github-dark-dimmed theme), YAML frontmatter as styled header (title, tag chips, date), internal link navigation, inline images.
- **Edit mode**: raw markdown textarea, Save/Cancel buttons, drag & drop image upload with auto-insert of markdown syntax, toast notifications for save feedback.
- **Mobile**: responsive single-column layout, hamburger menu, upload button fallback for touch devices.

## Bugs Found & Fixed

### 1. `python-multipart` missing
FastAPI's `UploadFile` requires `python-multipart`. Added to both `merlin.py` and `dashboard.py` inline deps.

### 2. Static file mount order
`/static` mount was catching `/static/notes/*` requests before the more specific mount. Fixed by mounting `/static/notes` BEFORE `/static` in dashboard.py.

### 3. Git "no changes" detection
`git commit` returns "no changes added to commit" (not "nothing to commit") when the staged content is identical. Fixed `git_ops.py` to check for both strings in combined stdout+stderr.

### 4. Media path resolution in nested notes
When viewing `kb/_index`, a `media/photo.png` image src was resolved to `kb/media/photo.png` (404). Fixed `_resolveImages()` to always resolve `media/` paths from the memory root, not relative to the current note's directory. Same fix applied to `_resolveLinks()` for media download links.

## Validation

- **Unit tests**: 316 passed, 1 pre-existing failure (unrelated discord test)
- **API tests**: all 7 endpoints tested including save flow, upload, 404, path traversal
- **Screenshots**: 8 screenshots across desktop/tablet/mobile for index, note view (with frontmatter, code blocks, no frontmatter, many links)
- **Live browser test**: confirmed on phone — image upload and display working after media path fix

## Files Changed

**New files (10):**
- `notes/__init__.py`, `routes.py`, `git_ops.py`, `frontmatter.py`
- `notes/templates/notes_index.html`, `notes_view.html`
- `notes/static/notes.css`, `notes.js`
- `epics/notes-editor/requirements.md`, `tasks.md`

**Modified files (5):**
- `dashboard.py` — mount notes router + static, add python-multipart dep
- `merlin.py` — add python-multipart to inline deps
- `templates/base.html` — "Notes" nav link in sidebar
- `static/dashboard.js` — sidebar active state supports nested paths (`/notes/kb/...`)
- `docs/dashboard-architecture.md` — documented notes module and API

## Commits

- `161f531` — Add notes editor to dashboard for manual KB access (main build)
- `e8d61c9` — Fix git commit detection for no-changes case
- `7f29466` — Fix media path resolution in notes viewer

## What's Left (Future)

From the epic's "out of scope" list:
- Create new notes from the dashboard (currently use Merlin's `kb_add.py` for auto-linking)
- Delete notes from dashboard
- WYSIWYG editor / real-time auto-save
- Video preview / YouTube embeds
- Full-text search in the editor
- Git history / diff viewer

The user also mentioned potentially wanting Obsidian alongside this for desktop editing — the formats are fully compatible since it's all standard markdown with YAML frontmatter.
