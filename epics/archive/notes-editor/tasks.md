# Notes Editor — Tasks

## Phase 1: Scaffolding & Backend API

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Create `notes/` module structure | done | `__init__.py`, `routes.py`, `git_ops.py`, `frontmatter.py`, `templates/`, `static/` |
| 2 | Mount notes router in `dashboard.py` | done | `app.include_router(notes_router)`, static at `/static/notes/` (mounted before `/static`) |
| 3 | Add "Notes" to sidebar in `base.html` | done | Nav link after "Logs" with pencil icon |
| 4 | `GET /api/notes` — list all memory files | done | Scan `memory/`, parse frontmatter, return path/title/summary/tags/mtime |
| 5 | `GET /api/notes/{path}` — read note | done | Return raw markdown string |
| 6 | `PUT /api/notes/{path}` — save + git | done | Write file, `git_ops.py` handles add/commit/push |
| 7 | `POST /api/notes/upload` — media upload | done | Save to `memory/media/`, return relative path |

## Phase 2: View Mode

| # | Task | Status | Notes |
|---|------|--------|-------|
| 8 | `notes_view.html` template + route | done | Extends base.html, view mode layout |
| 9 | Markdown rendering (marked.js) | done | Client-side, CDN |
| 10 | Syntax highlighting (highlight.js) | done | github-dark-dimmed theme |
| 11 | YAML frontmatter header | done | Title, tags as chips, date — parsed in JS |
| 12 | Internal link resolution | done | Rewrite `[text](file.md)` hrefs to `/notes/kb/file` |
| 13 | Image rendering | done | Inline images, resolve `media/` paths |

## Phase 3: Command Palette

| # | Task | Status | Notes |
|---|------|--------|-------|
| 14 | `notes_index.html` + route | done | Landing page: recent notes, tag cloud, stats |
| 15 | Command palette UI | done | Overlay, search input, keyboard nav (up/down/enter) |
| 16 | Fuzzy search (fuse.js) | done | Search across path, title, summary, tags |
| 17 | Keyboard shortcut (Ctrl+K) | done | Opens palette from any notes page, Escape to close |

## Phase 4: Edit Mode & Upload

| # | Task | Status | Notes |
|---|------|--------|-------|
| 18 | Edit/Save/Cancel UI | done | Toggle view ↔ edit, raw markdown textarea |
| 19 | Save flow (API + toast) | done | PUT to API, green/yellow/red toast feedback |
| 20 | Drag & drop image upload | done | Drop zone on editor, upload via API, insert markdown |
| 21 | File input fallback (mobile) | done | Upload button shown on mobile viewports |

## Phase 5: Polish & Validation

| # | Task | Status | Notes |
|---|------|--------|-------|
| 22 | `notes.css` — dark theme styling | done | Uses existing CSS variables from dashboard |
| 23 | Mobile responsive | done | Single column, full-width palette, readable on phone |
| 24 | Screenshot validation | done | Desktop + mobile for index and note view |
| 25 | Tests | done | 316 passed (existing), 1 pre-existing failure (discord) |

## Phase 6: Post-V1 Features

| # | Task | Status | Notes |
|---|------|--------|-------|
| 26 | Create note via command palette | done | "+ Create {path}" option at top of results when path doesn't exist, opens editor with frontmatter template |
| 27 | Delete note from dashboard | done | Delete button in toolbar, browser confirm(), `DELETE /api/notes/{path}`, git rm + commit + push |
| 28 | Cancel redirects to index for new notes | done | `_isNew` flag, cancel on unsaved note goes to `/notes` instead of re-rendering |

## Phase 7: Mobile & UX Fixes

| # | Task | Status | Notes |
|---|------|--------|-------|
| 29 | Fix iOS auto-zoom on editor textarea | done | Set font-size to 16px on mobile (iOS zooms inputs < 16px) |
| 30 | Fix cancel not hiding editor/buttons | done | Moved UI state changes before markdown rendering in `_renderView()` |
| 31 | Replace floating hamburger with static top bar | done | Hamburger was `position: fixed` overlapping content; now in normal-flow `.mobile-topbar` |

## Fixes Applied

- Added `python-multipart` to `merlin.py` inline deps (required by `UploadFile`)
- Mounted `/static/notes` before `/static` in dashboard.py (route priority)
