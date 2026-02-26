# File Browser — Tasks

## Phase 1: Skeleton & Backend

- [x] 1.1 Create `files/__init__.py` — export router + FILES_STATIC_DIR
- [x] 1.2 Create `files/fs_helpers.py` — `validate_path()`, `list_directory()`, `read_text_file()`, type detection constants
- [x] 1.3 Create `files/routes.py` — page routes (`/files`, `/files/{path}`) + API endpoints (`browse`, `content`, `raw`)
- [x] 1.4 Register module in `dashboard.py` — include_router + mount statics
- [x] 1.5 Add "Files" nav link in `templates/base.html` sidebar
- [x] 1.6 Write tests for `fs_helpers.py` — path validation, directory listing, text reading, blocked paths, edge cases

## Phase 2: Directory Listing View

- [x] 2.1 Create `files/templates/files.html` — SPA template with dir listing view
- [x] 2.2 Create `files/static/files.js` — IIFE scaffold, state, DOM refs, routing
- [x] 2.3 Implement directory loading + rendering (entry rows with icons, names, sizes, times)
- [x] 2.4 Implement breadcrumb navigation (clickable path segments)
- [x] 2.5 Create `files/static/files.css` — directory listing styles, breadcrumbs, entries
- [x] 2.6 Implement `history.pushState` routing + popstate handler

## Phase 3: File Viewer — Text

- [x] 3.1 Add file viewer view to template (back button, file meta, wrap toggle, download link)
- [x] 3.2 Implement text file loading + rendering (table with line numbers + highlight.js)
- [x] 3.3 Add wrap toggle functionality
- [x] 3.4 Add file viewer CSS (file table, line numbers, syntax highlighting support)

## Phase 4: File Viewer — Images & Binary

- [x] 4.1 Implement image preview (centered img via `/api/files/raw`, size info)
- [x] 4.2 Implement binary/unknown file info card + download button
- [x] 4.3 Add image preview + binary info CSS

## Phase 5: Polish & Mobile

- [x] 5.1 Mobile responsive CSS (768px breakpoint, edge-to-edge, hidden metadata)
- [x] 5.2 Handle edge cases: empty dirs, permission denied, large files, special chars in names
- [x] 5.3 Handle dotfiles (show them, dimmed opacity)
- [x] 5.4 Screenshot validation across viewports
