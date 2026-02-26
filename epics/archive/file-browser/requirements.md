# File Browser — Requirements

## Vision

A mobile-first file browser integrated into the Merlin dashboard. Browse the full filesystem, preview images inline, view text files with syntax highlighting. Read-only Phase 1, designed for future expansion into a full file manager.

## Goals

1. **Full filesystem browsing** — navigate any directory on the system
2. **Image preview** — inline preview for PNG, JPG, GIF, SVG, WebP
3. **Text file viewing** — syntax-highlighted code with line numbers (highlight.js)
4. **Binary file info** — file metadata + download link for non-text/image files
5. **Mobile-first** — touch-friendly, dark theme, matching dashboard conventions
6. **Deep linking** — bookmarkable URLs, browser back/forward

## Non-Goals (Phase 1)

- File editing, creation, deletion, renaming
- File upload
- Git integration (status, commit)
- Search/filter within directory listings
- Thumbnail generation for images

## Future (Phase 2+)

- Edit text files (CodeMirror or textarea)
- Create/delete/rename files and directories
- Upload files via drag & drop
- Git status indicators in directory listing
- Directory tree sidebar
- Search within files

## UX Design

### Two Views

**View 1 — Directory Listing**
```
┌──────────────────────────────────────┐
│ ☰                                    │ ← mobile topbar
├──────────────────────────────────────┤
│ Files                                │
│                                      │
│ / home / user / merlin /             │ ← breadcrumbs (clickable)
│                                      │
│ ┌──────────────────────────────────┐ │
│ │ 📁 commits/         —    3h ago │ │
│ │ 📁 docs/             —    1d ago │ │
│ │ 📁 epics/            —    2m ago │ │
│ │ 📄 CLAUDE.md     4.2KB   1d ago │ │
│ │ 🖼 screenshot.png 85KB   5h ago │ │
│ │ 📄 dashboard.py  12KB   3h ago │ │
│ └──────────────────────────────────┘ │
└──────────────────────────────────────┘
```

- Directories sorted first, then files alphabetically
- Icons: folder (blue), text file, image file (green), generic file
- Metadata: size (files only), relative time
- 44px min-height rows (touch targets)
- Click folder → navigate in
- Click file → open viewer

**View 2 — File Viewer**
```
┌──────────────────────────────────────┐
│ ← dashboard.py           Wrap  ⬇    │ ← back btn, path, wrap toggle, download
├──────────────────────────────────────┤
│  1 │ """Merlin dashboard — FastAPI..│ │
│  2 │                                │ │ ← syntax highlighted code
│  3 │ import asyncio                 │ │    with line numbers
│  4 │ from pathlib import Path       │ │
│  5 │ ...                            │ │
└──────────────────────────────────────┘
```

For images:
```
┌──────────────────────────────────────┐
│ ← screenshot.png                ⬇    │
├──────────────────────────────────────┤
│                                      │
│         ┌──────────────┐             │
│         │              │             │
│         │   (image)    │             │
│         │              │             │
│         └──────────────┘             │
│                                      │
│       85.2 KB · 1920×1080           │ ← size + dimensions
└──────────────────────────────────────┘
```

For binary/unknown files:
```
┌──────────────────────────────────────┐
│ ← archive.tar.gz               ⬇    │
├──────────────────────────────────────┤
│                                      │
│            📄                        │
│       archive.tar.gz                │
│       12.5 MB · .tar.gz            │
│                                      │
│       [ Download ]                   │
│                                      │
└──────────────────────────────────────┘
```

### Navigation

- **Breadcrumbs**: clickable path segments at top of directory view
- **Back button**: in file viewer, returns to parent directory
- **Browser history**: `history.pushState` for all navigation, popstate handler
- **URL scheme**: `/files` = root, `/files/home/user/merlin/CLAUDE.md` = deep link

### Responsive (768px breakpoint)

- Mobile: hide size/mtime columns, edge-to-edge code blocks, reduced padding
- Desktop: full metadata, bordered containers, max-width 960px

## Architecture

### Module Structure

```
merlin-bot/files/
├── __init__.py          # Exports: router, FILES_STATIC_DIR
├── routes.py            # FastAPI APIRouter: page route + 3 API endpoints
├── fs_helpers.py        # Filesystem logic: validate, list, read, type detection
├── templates/
│   └── files.html       # Single SPA template (2 views)
└── static/
    ├── files.css
    └── files.js
```

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `GET /files` | page | SPA shell |
| `GET /files/{path:path}` | page | SPA shell (deep link) |
| `GET /api/files/browse?path=` | API | List directory contents |
| `GET /api/files/content?path=` | API | Read text file content (up to 2MB) |
| `GET /api/files/raw?path=` | API | Serve raw file (FileResponse for images/downloads) |

Query params for paths (not path params) to avoid URL encoding issues.

### Security

- **Path validation**: resolve symlinks and `..` before checking; all access through `validate_path()`
- **Blocked paths**: `/proc/`, `/sys/`, `/dev/` — pseudo-filesystems that can expose sensitive data or hang
- **Read-only**: no write endpoints in Phase 1
- **Auth**: wrapped in `Depends(require_auth)` like all dashboard routes

### Integration Points

- `dashboard.py`: `include_router()` + `mount()` for statics
- `templates/base.html`: sidebar nav link (Lucide `file` icon)
- highlight.js: CDN (already used by commits module, browser-cached)

## Acceptance Criteria

- [ ] Browse any directory on the filesystem
- [ ] Breadcrumb navigation works (click any segment)
- [ ] Browser back/forward works
- [ ] Deep links work (paste URL, reload page)
- [ ] Text files render with syntax highlighting + line numbers
- [ ] Images render inline with size info
- [ ] Binary files show info + download link
- [ ] Download button works for any file
- [ ] Wrap toggle works for text files
- [ ] Permission denied handled gracefully (not a crash)
- [ ] Empty directories show empty state
- [ ] Mobile layout works (768px breakpoint)
- [ ] Blocked paths (/proc, /sys, /dev) return 403
- [ ] Existing tests still pass
- [ ] Screenshot validation across viewports
