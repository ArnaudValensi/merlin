# 2026-02-09 — Notes Editor: Post-V1 Features & Fixes

## What Was Done

Added note creation and deletion to the dashboard notes editor, plus several mobile UX fixes.

## New Features

### Note Creation via Command Palette
- Type a non-existent path in the palette (e.g., `kb/docker-tips`)
- A green "+ Create kb/docker-tips" option appears at the top of results
- Clicking it opens the editor with pre-filled frontmatter: title (deslugified from filename), today's date, empty tags/related/summary
- Save creates the file, commits, and pushes
- Cancel on a new unsaved note redirects to `/notes` index

**Files:** `notes/routes.py` (added `?new=1` support), `notes/static/notes.js` (Palette `_render` + `_create`, NoteView `init` new note handling, `_isNew` flag)

### Note Deletion
- Delete button in view mode toolbar (red text, subtle `btn-danger` style)
- Browser `confirm()` dialog before deleting
- `DELETE /api/notes/{path}` endpoint: validates path, `git rm`, commit, push
- Redirects to `/notes` after deletion
- Button hidden during edit mode and for new unsaved notes
- No protection on any files — user is trusted

**Files:** `notes/git_ops.py` (added `delete_and_push`), `notes/routes.py` (added DELETE endpoint), `notes/static/notes.js` (added `delete()` method), `notes/static/notes.css` (`btn-danger` style), `notes/templates/notes_view.html` (delete button)

## Bug Fixes

### iOS Auto-Zoom on Editor (commit `86b63f7`)
iOS Safari auto-zooms input fields with font-size < 16px. The editor textarea was 14px. Fixed by setting `font-size: 16px` in the mobile media query.

### Cancel Not Hiding Editor/Buttons (commit `e261c86`)
After clicking Cancel, the textarea and buttons stayed visible. Root cause: if `marked.parse()` or highlight.js threw during re-render, the code never reached the lines that hide the editor and update the toolbar. Fixed by moving UI state changes (hide editor, show content, update toolbar) before markdown rendering in `_renderView()`.

### Floating Hamburger on Mobile (commit `c9bfc9f`)
The hamburger menu was `position: fixed`, floating over page content and obscuring text when scrolling. Replaced with a static `.mobile-topbar` div that scrolls with the page. The main content padding-top was reduced from 60px to 16px since the topbar is now in normal flow.

## Attempted & Reverted

### Disable iOS Zoom on Notes Pages
Tried both `<meta viewport maximum-scale=1 user-scalable=no>` (iOS ignores since iOS 10) and CSS `touch-action: pan-x pan-y` (worked but blocked pinch-to-zoom on images, which the user wanted). Reverted entirely — the 16px font-size fix handles the main annoyance.

## Commits

- `86b63f7` — Fix iOS auto-zoom on notes editor textarea
- `e261c86` — Fix cancel button not hiding after edit mode
- `543ba30` — Add note creation via command palette
- `c9bfc9f` — Replace floating hamburger with static top bar on mobile
- `8b7e482` — Add note deletion from dashboard
- `e693724` — Redirect to index when canceling a new unsaved note
