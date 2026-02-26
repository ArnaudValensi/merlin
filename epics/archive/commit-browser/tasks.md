# Commit Browser — Tasks

## Phase 1: Scaffolding & Backend

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Create `commits/` module structure | done | `__init__.py`, `routes.py`, `git_parser.py`, `templates/`, `static/` |
| 2 | Mount commits router in `dashboard.py` | done | Same pattern as notes module |
| 3 | Add "Commits" to sidebar in `base.html` | done | Nav link with git icon |
| 4 | `git_parser.py` — git log parsing | done | Run `git log`, parse pipe-delimited output into dicts |
| 5 | `test_git_parser.py` — log parsing tests | done | 6 tests: single/multi commit, empty, special chars, pipes, no stats |
| 6 | `git_parser.py` — commit detail + diff parsing | done | `git show -p`, parse unified diff into structured data |
| 7 | `test_git_parser.py` — diff parsing tests | done | 9 tests: add/del/modify/rename, binary, multi-file, multi-hunk, line numbers, empty |
| 8 | `git_parser.py` — full file + gutter data | done | `git show <hash>:<path>` + `git diff` for gutter markers |
| 9 | `test_git_parser.py` — gutter computation tests | done | 8 tests: no diff, additions, deletions, modifications, multi-hunk, all-new, multi-del, empty |
| 10 | `GET /api/commits` — paginated commit list | done | Query params: skip, limit, search, since, until |
| 11 | `GET /api/commits/<hash>` — commit metadata | done | File stats, message, author, date |
| 12 | `GET /api/commits/<hash>/diff` — parsed diff | done | Structured hunks with line types |
| 13 | `GET /api/commits/<hash>/file/<path>` — file + gutters | done | Full file content with gutter annotations |
| 14 | `test_commits_routes.py` — API endpoint tests | done | 18 tests: all 4 endpoints, filtering, pagination, validation, pages |

## Phase 2: Commit List View

| # | Task | Status | Notes |
|---|------|--------|-------|
| 15 | `commits.html` template (extends base.html) | done | Single template with all 3 views, JS view switching, highlight.js CDN |
| 16 | Commit list rendering | done | Feed layout: hash, message, author, time ago, +/- stats, tap to enter |
| 17 | "Load more" pagination | done | Button at bottom, appends to list, hidden when fewer than PAGE_SIZE results |
| 18 | Search by message | done | Text input, 300ms debounced API call, resets list |
| 19 | Date range filter | done | Since/until date inputs with dark-mode styling |
| 20 | Screenshot validation — commit list | done | Mobile (375x812) + desktop (1280x800) — layout, sidebar, filters all correct |

## Phase 3: Commit Diff View

| # | Task | Status | Notes |
|---|------|--------|-------|
| 21 | Diff view layout | done | Back button, commit header (message, hash, author, time), file sections |
| 22 | File list header (collapsible) | done | File names + M/A/D/R status badges + line stats, tap to scroll to section |
| 23 | Hunk rendering | done | Context (dimmed), additions (green bg), deletions (red bg), old/new line numbers |
| 24 | "Full file" button per file | done | Opens view 3 for that file, hidden for deleted files |
| 25 | Screenshot validation — diff view | done | File list panel, hunk colors, line numbers verified on both viewports |

## Phase 4: Full File with Gutters

| # | Task | Status | Notes |
|---|------|--------|-------|
| 26 | Full file rendering with syntax highlighting | done | highlight.js per-line, horizontal scroll, monospace 13px |
| 27 | Gutter bars (added/deleted/modified) | done | Green/red/blue 4px left-edge markers, clickable for del/mod |
| 28 | FAB for gutter navigation | done | Prev/next buttons, counter (1/13), auto-open deletions on jump |
| 29 | Deletion expansion | done | Tap red/blue gutter → inline red block with deleted lines, toggle |
| 30 | Wrap toggle | done | Button toggles pre-wrap, active state styling (blue border) |
| 31 | Screenshot validation — file view | done | Syntax highlighting, FAB, gutter nav, deletion expansion, wrap toggle verified |

## Phase 5: Polish & Integration

| # | Task | Status | Notes |
|---|------|--------|-------|
| 32 | `commits.css` — mobile-first dark theme | done | Edge-to-edge code on mobile, touch targets, FAB, responsive breakpoint |
| 33 | URL management (history.pushState) | done | pushState + popstate handler, deep links for all 3 views |
| 34 | End-to-end validation | done | 12 screenshots across mobile+desktop: list, diff, filelist, file, gutter, wrapped |
| 35 | Run full test suite | done | All 477 tests pass (35 parser + 18 routes + 424 existing) |
