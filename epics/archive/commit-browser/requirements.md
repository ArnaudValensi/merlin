# Epic: Commit Browser

## Overview

Add a mobile-first commit browser to the Merlin dashboard. Review git commits and diffs from your phone with a UX comparable to GitHub's commit view — plus a full-file gutter view inspired by neovim's git-gutter plugin.

## Goals

1. **Phone-first code review** — Browse commits, read diffs, and navigate changes from a phone browser with touch-friendly UX
2. **Three-view navigation** — Commit List → Commit Diff → Full File with Gutters, each level progressively more detailed
3. **Gutter navigation** — In full-file view, jump between changes with a floating button instead of scrolling through hundreds of lines
4. **Deletion visibility** — Expand deleted lines inline at red gutters, so you see what was removed without switching views
5. **Integrated** — Part of the dashboard, same process, same auth, same port, same dark theme

## UX Design

### View 1: Commit List

Feed of commits on the current branch. Each row shows:
- Short hash (monospace), commit message, time ago
- Tap → enters commit diff view

**Pagination:** Loads 30–50 commits initially. "Load more" button at the bottom (not infinite scroll — explicit is better on mobile).

**Filters:**
- Search by commit message (text input)
- Date range (since/until)

### View 2: Commit Diff

Like GitHub's commit page:

**File list header** (collapsible):
- Lists all changed files with status (M/A/D/R) and +/- line counts
- Tap a file name → scrolls to that file's diff section
- Collapsed by default on mobile to save space

**Per-file diff sections:**
- File path as section header with a "Full file" button → enters view 3
- Hunks with 3 lines of context (dimmed), additions (green background), deletions (red background)
- Unified diff format (not side-by-side — mobile screens are too narrow)

### View 3: Full File with Gutters

The entire file at the commit's version, with syntax highlighting (highlight.js).

**Gutter bars** on the left edge:
- **Green bar** — added line
- **Red bar** — deleted or replaced line
- **Blue bar** — modified line

**Floating Action Button (FAB)** — bottom-right, sticky:
- Tap = jump to next gutter
- Second button (or long-press) = jump to previous gutter
- Shows current/total count (e.g., "3/7")

**Deletion expansion:**
- Tap a red gutter bar → inline red block expands below showing the deleted lines
- Tap again → collapses
- If the current gutter is a deletion and you jump to it via FAB, the expansion could auto-open

**Code display:**
- Horizontal scroll by default (like GitHub)
- Toggle button for line wrapping
- Monospace 13px
- Line numbers: small, dimmed, always visible

**Syntax highlighting:** highlight.js with `github-dark-dimmed` theme (already used by notes editor).

### Navigation

- **Back button** at each view level (top-left)
- Browser back also works (use `history.pushState` for URL management)
- URL structure:
  - `/commits` — commit list
  - `/commits/<hash>` — commit diff
  - `/commits/<hash>/file/<path>` — full file with gutters

## Architecture

### Module Structure

Self-contained module following the `notes/` pattern:

```
merlin-bot/
├── commits/
│   ├── __init__.py            # Exports router + COMMITS_STATIC_DIR
│   ├── routes.py              # Pages + API endpoints
│   ├── git_parser.py          # Git command wrappers + output parsing
│   ├── templates/
│   │   └── commits.html       # Single template, JS switches between views
│   └── static/
│       ├── commits.css        # Dark theme, mobile-first
│       └── commits.js         # View management, diff rendering, gutter logic
```

Integration in `dashboard.py`:
```python
from commits import router as commits_router, COMMITS_STATIC_DIR
app.include_router(commits_router, dependencies=[Depends(require_auth)])
app.mount("/static/commits", StaticFiles(directory=str(COMMITS_STATIC_DIR)))
```

### API Endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /api/commits?skip=0&limit=50&search=&since=&until=` | Paginated commit list: `[{hash, short, message, author, date, files_changed, insertions, deletions}]` |
| `GET /api/commits/<hash>` | Single commit metadata + file stats: `{hash, message, author, date, files: [{path, status, insertions, deletions}]}` |
| `GET /api/commits/<hash>/diff` | Parsed unified diff: `{files: [{path, status, hunks: [{header, lines: [{type, content, old_no, new_no}]}]}]}` |
| `GET /api/commits/<hash>/file/<path>` | Full file + gutter data: `{content, lines: [{no, content, gutter: null|"added"|"deleted"|"modified", deleted_lines: [...]}]}` |

### Git Commands (Backend)

| Purpose | Command |
|---------|---------|
| Commit list | `git log --format='%H\|%h\|%an\|%ai\|%s' --shortstat --skip=N --max-count=M` |
| Commit metadata | `git show --format='%H\|%an\|%ai\|%s\|%b' --stat --name-status <hash>` |
| Unified diff | `git show -p --format= <hash>` |
| Full file at commit | `git show <hash>:<path>` |
| File diff (for gutters) | `git diff <hash>^..<hash> -- <path>` |

### Frontend Libraries (CDN)

| Library | Purpose |
|---------|---------|
| highlight.js | Syntax highlighting in full file view (already in stack) |

No additional CDN dependencies needed. Diff parsing and gutter logic are pure JS.

## Mobile Considerations

- **Touch targets:** All buttons/links minimum 44x44px
- **FAB:** 56px diameter, 16px from bottom-right edge, high z-index
- **Code scroll:** `overflow-x: auto` on code blocks, `-webkit-overflow-scrolling: touch`
- **File list:** Collapsible to save vertical space
- **Font size:** 13px monospace for code (readable on mobile, fits ~35-40 chars)
- **Line numbers:** 12px, dimmed (`text-muted`), narrow column
- **No horizontal padding waste:** Code blocks go edge-to-edge on mobile

## V1 Scope

### In Scope
- [ ] Commit list with pagination and search
- [ ] Commit diff view with file list and hunks
- [ ] Full file view with syntax highlighting
- [ ] Gutter bars (added/deleted/modified)
- [ ] FAB for jumping between gutters
- [ ] Deletion expansion on red gutters
- [ ] Horizontal scroll + wrap toggle for code
- [ ] Mobile-first responsive design
- [ ] Dashboard integration (sidebar, auth, theme)
- [ ] URL-based navigation with browser history

### Out of Scope (future)
- [ ] Branch selector (currently single `master` branch)
- [ ] Expand context lines (GitHub's "show more" between hunks)
- [ ] Side-by-side diff (desktop only, would need wide viewport)
- [ ] Blame view
- [ ] Commit search by file path
- [ ] Swipe gestures for navigation

## Acceptance Criteria

### Must Have
- [ ] `/commits` accessible from dashboard sidebar
- [ ] Commit list shows recent commits with hash, message, time
- [ ] Tapping a commit shows diff with all changed files
- [ ] Diff shows additions (green) and deletions (red) with context
- [ ] "Full file" button opens file with gutter markers
- [ ] FAB jumps between gutters in full file view
- [ ] Red gutters expand to show deleted lines
- [ ] Usable on a phone (375px viewport)
- [ ] Same auth as rest of dashboard

### Should Have
- [ ] Search commits by message text
- [ ] Date range filter
- [ ] File list header in diff view (collapsible)
- [ ] Line numbers in full file view
- [ ] Syntax highlighting in full file view
- [ ] Wrap toggle for code

### Nice to Have
- [ ] FAB shows change counter (e.g., "3/7")
- [ ] Auto-open deletion expansion when FAB lands on red gutter
- [ ] Smooth scroll animation when jumping between gutters

## References

- [Dashboard architecture](../../docs/dashboard-architecture.md) — Theme, CSS conventions, JS patterns, how to add pages
- [Notes editor epic](../notes-editor/) — Module structure reference
- [GitHub commit view](https://github.com) — UX inspiration for diff view
- [neovim git-gutter](https://github.com/lewis6991/gitsigns.nvim) — Gutter bar inspiration
