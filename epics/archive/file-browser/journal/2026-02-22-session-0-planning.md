# Session 0 — Planning & Context Gathering

**Date:** 2026-02-22
**Goal:** Design the file browser epic, gather all architectural context

## What Was Done

### Commit Browser Polish (before this epic)

Two commits were made to polish the commit browser before archiving it:

1. **`c340275` — Diff mode toggle**: Replaced per-line tap-to-toggle with a global "Diff" button in the FAB navigation group. Deleted lines are now rendered as proper `<tr>` table rows interleaved before modified lines (aligned like the diff view), toggled all at once via CSS class `.diff-mode`.

2. **`875f8ac` — Click hunk to open file view**: Hunk headers (`@@ ... @@`) in the commit diff view are now clickable. Clicking navigates to the full file view, enables diff mode, and scrolls to the matching hunk. Uses closest-distance matching on line numbers since the hunk header start line includes context lines before the first actual change.

The commit-browser epic was archived to `epics/archive/`.

### File Browser Planning

Explored the codebase to understand:
- **Module pattern**: every dashboard module follows `__init__.py` + `routes.py` + `templates/` + `static/` structure
- **Commits module**: best reference — SPA-style with JS view switching, `history.pushState`, highlight.js
- **Notes module**: has `FileResponse` pattern for serving raw files, media upload with path validation
- **Dashboard architecture**: `docs/dashboard-architecture.md` has all CSS variables, spacing tokens, button patterns, typography, icon conventions

### Key Architectural Decisions

1. **Query params for API paths** (`?path=/some/file`) instead of path params — avoids URL encoding issues
2. **`/api/files/browse` handles both dirs and files** — returns type info, JS decides which view
3. **2MB text file limit** — prevents memory/DOM issues, shows truncation notice + download for larger
4. **Block `/proc/`, `/sys/`, `/dev/`** — pseudo-filesystems that can hang or expose sensitive data
5. **Highlight.js via CDN** — already cached from commits module
6. **Self-contained CSS** — duplicate file table styles rather than coupling to commits module

## Files Created

- `epics/file-browser/requirements.md` — Full spec with UX mockups, architecture, acceptance criteria
- `epics/file-browser/thinking.md` — Research context, design decisions, reference file paths
- `epics/file-browser/tasks.md` — 20 tasks across 5 phases

## Key Reference Files for Implementation

| File | What to reuse |
|------|---------------|
| `merlin-bot/commits/__init__.py` | Module init pattern (2 lines) |
| `merlin-bot/commits/routes.py` | Route structure, SPA page routes, path validation |
| `merlin-bot/commits/static/commits.js` | SPA view switching, pushState routing, highlight.js integration, file table rendering with line numbers |
| `merlin-bot/commits/static/commits.css` | File table styles, back button, wrap toggle, mobile breakpoints, edge-to-edge on mobile |
| `merlin-bot/commits/templates/commits.html` | Template structure, CDN includes, block layout |
| `merlin-bot/notes/routes.py` | FileResponse pattern for raw file serving |
| `merlin-bot/dashboard.py` | Router registration + static mount (lines ~148-172) |
| `merlin-bot/templates/base.html` | Sidebar nav link format, icon convention |
| `docs/dashboard-architecture.md` | CSS variables, spacing, buttons, typography, icons |

## Module Structure to Create

```
merlin-bot/files/
├── __init__.py          # Exports: router, FILES_STATIC_DIR
├── routes.py            # Page routes + 3 API endpoints
├── fs_helpers.py        # validate_path, list_directory, read_text_file
├── templates/
│   └── files.html       # SPA template (dir listing + file viewer)
└── static/
    ├── files.css
    └── files.js
```

Plus edits to:
- `merlin-bot/dashboard.py` — register router + mount statics
- `merlin-bot/templates/base.html` — add "Files" sidebar nav link

## API Design

| Endpoint | Purpose |
|----------|---------|
| `GET /files`, `GET /files/{path:path}` | SPA page shell |
| `GET /api/files/browse?path=` | List dir or return file info |
| `GET /api/files/content?path=` | Read text file (up to 2MB) |
| `GET /api/files/raw?path=` | Serve raw file (FileResponse) |
