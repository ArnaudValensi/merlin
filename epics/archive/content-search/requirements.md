# Epic: Content Search (Palette Grep)

## Overview

Add full-text content search to the notes command palette, inspired by Telescope/fzf live grep in Neovim. Users type `/` followed by a search term in the existing palette, and results show matching lines from note contents with file path, line number, and context.

## Goals

1. **Find text across all notes** — Search inside note content, not just metadata
2. **Integrated in existing palette** — No new UI, just a mode switch via `/` prefix
3. **Fast and live** — Results update as you type (debounced)
4. **Keyboard-driven** — Navigate results with arrows, Enter opens the note
5. **Mobile-friendly** — Same experience on phone, works within existing palette

## UX Design

### Mode Switching: `/` Prefix

The existing command palette (Ctrl+K) gains a content search mode:

- **No prefix** → File search (current behavior, client-side fuse.js on metadata)
- **`/` prefix** → Content search (server-side grep, debounced API call)

The placeholder text should hint at this: `Search notes... (/ to search content)`

### File Search Mode (unchanged)

Current behavior, no changes:
- Fuzzy matches on path, title, summary, tags via fuse.js
- Client-side, instant
- Shows: path, summary, tag chips
- "+" Create option for non-existent paths

### Content Search Mode (new)

Activated when input starts with `/`:

**Input behavior:**
- `/` alone → show hint text: "Type to search note contents..."
- `/dub bass` → search for "dub bass" across all notes
- Debounce: 300ms after last keystroke before firing API call
- Show a subtle loading indicator while searching

**Result format:**
Each result shows one matching line:
```
kb/tb-303-beyond-acid-techniques                    :42
  ...pushing the 303 beyond acid — dub bass, ambient textures...
```

- **Top line:** file path (left) + line number (right, muted)
- **Bottom line:** the matching line, trimmed, with the search term highlighted (bold or accent color)
- Results grouped by file (multiple matches in the same file appear consecutively)
- Max ~50 results to keep the response fast

**Navigation:**
- Arrow keys (up/down) navigate results, same as file search mode
- Enter on a result → opens `/notes/{path}` (the note view page)
- Escape closes the palette
- No "create" option in content search mode

**Empty states:**
- No results → "No matches found"
- Query too short (< 2 chars after `/`) → "Type at least 2 characters..."

### Visual Distinction

Content search results should look distinct from file search results so the user knows which mode they're in:

- File results: show path + summary + tags (current)
- Content results: show path + line number + matching line with highlight
- Use a different left-border or icon to distinguish (optional, keep subtle)
- A small mode indicator in the palette (e.g., "CONTENT" badge or just the `/` prefix being visually distinct)

## Architecture

### API Endpoint

```
GET /api/notes/search?q=<query>
```

**Request:**
- `q` — Search query string (min 2 characters)

**Response:**
```json
{
  "query": "dub bass",
  "results": [
    {
      "path": "kb/tb-303-beyond-acid-techniques",
      "title": "Beyond Acid — TB-303 Sound Design Techniques",
      "line_number": 42,
      "line": "pushing the 303 beyond acid — dub bass, ambient textures, percussion",
      "context_before": "## Deep Bass Techniques",
      "context_after": "sub-bass, and effects patching"
    },
    ...
  ],
  "total": 12,
  "truncated": false
}
```

**Implementation (server-side, in `notes/routes.py`):**
1. Read all `.md` files in `memory/` (same scan as `api_list_notes`, exclude same files)
2. For each file, split content into lines
3. Case-insensitive substring search (not regex — keep it simple and safe)
4. For each matching line, return: path, title (from frontmatter), line number, the line, and 1 line of context before/after
5. Limit to 50 results total
6. Skip frontmatter section (`---` to `---`) from line numbering? Or include it? → **Include it** (user might want to search tags/metadata too via content search)

**Performance:**
- 21 files, 116K total — trivially fast to scan on every request
- No indexing needed at this scale
- If the KB grows to hundreds of files, consider caching file contents in memory on startup (but not needed for V1)

### Client-Side Changes

**File: `notes/static/notes.js` — `Palette` object**

Modify `_onInput()`:
```javascript
_onInput() {
    const raw = this._input.value;
    const query = raw.trim();

    if (raw.startsWith('/')) {
        // Content search mode
        const searchQuery = raw.slice(1).trim();
        this._contentSearch(searchQuery);
    } else {
        // File search mode (existing behavior)
        if (!query) { this._render(this._notes, ''); return; }
        const results = this._fuse.search(query).map(r => r.item);
        this._selectedIndex = 0;
        this._render(results, query);
    }
}
```

New method `_contentSearch(query)`:
- If query.length < 2, show hint
- Debounce 300ms (cancel previous pending request)
- Fetch `GET /api/notes/search?q=...`
- Render results with `_renderContentResults(data)`

New method `_renderContentResults(data)`:
- Different HTML structure from file results
- Show path + line number + matching line with highlights
- Same `.palette-item` class for keyboard navigation

**Highlighting:** Wrap matching text in `<mark>` or `<strong>`. Use case-insensitive replacement:
```javascript
line.replace(new RegExp(`(${escapeRegex(query)})`, 'gi'), '<mark>$1</mark>')
```

### CSS Changes

**File: `notes/static/notes.css`**

New classes needed:
- `.palette-content-item` — Layout for content search results (path + line number on top, matching line below)
- `.palette-line-number` — Muted, right-aligned line number
- `.palette-match-line` — The matching line text, monospace, slightly smaller
- `.palette-match-line mark` — Highlight styling for the matched term
- `.palette-hint` — Hint text style ("Type to search note contents...")
- `.palette-loading` — Subtle loading state (could be as simple as dimming results)

## Code Structure

All changes are in the existing notes module — no new files needed:

```
notes/
├── routes.py         # Add GET /api/notes/search endpoint
├── static/
│   ├── notes.js      # Modify Palette: _onInput, add _contentSearch, _renderContentResults
│   └── notes.css     # Add content result styles
└── templates/
    ├── notes_index.html   # Update placeholder text
    └── notes_view.html    # Update placeholder text
```

## References

- **Design system:** `docs/dashboard-architecture.md` → "Design System" section for colors, spacing, typography, button/component patterns
- **Notes editor epic:** `epics/notes-editor/requirements.md` → Original palette design, architecture, file structure
- **Notes editor journal:** `epics/notes-editor/journal/2026-02-09-post-v1-features.md` → Recent changes (creation, deletion, CodeMirror, icons)
- **Current palette code:** `merlin-bot/notes/static/notes.js` → `Palette` object (lines 32–195)
- **Current palette CSS:** `merlin-bot/notes/static/notes.css` → `.palette-*` classes (lines 4–101)
- **Routes:** `merlin-bot/notes/routes.py` → existing API endpoints, `_validate_path`, `MEMORY_DIR`, `EXCLUDE_FILES`
- **Frontmatter parser:** `merlin-bot/notes/frontmatter.py` → `parse_frontmatter()` for extracting titles

## Key Design System Rules

From `docs/dashboard-architecture.md`:

- **Font:** Geist (Google Fonts) for all UI text. Monospace (`SF Mono, Fira Code`) only for code/technical data.
- **Colors:** Use CSS variables only (`--text-primary`, `--text-muted`, `--accent-blue`, etc.). Never hardcode.
- **Spacing:** 4px tight, 8px normal, 16px section. Palette uses 6px result padding, 14px input padding.
- **Interactive elements:** `bg-card` background + `border` for buttons and inputs. `bg-hover` on hover.
- **Icons:** Lucide, inline SVG, 18px standard size.
- **Responsive:** Single breakpoint at 768px. Mobile gets full-width palette.

## Scope

### In Scope
- [ ] `/` prefix triggers content search mode in existing palette
- [ ] `GET /api/notes/search?q=` endpoint with case-insensitive substring search
- [ ] Results show: path, line number, matching line with highlighted term
- [ ] 1 line of context before/after each match
- [ ] Debounced input (300ms)
- [ ] Keyboard navigation (arrows + Enter)
- [ ] Max 50 results
- [ ] Loading state while searching
- [ ] Hint text when `/` typed but query too short
- [ ] Updated placeholder text on palette input
- [ ] Works on mobile

### Out of Scope (future)
- Regex search
- Search result preview pane (Telescope-style split view)
- Scroll-to-match when opening a note from content search
- Search history
- Indexing / caching for performance (unnecessary at current scale)
- Searching across non-markdown files

## Acceptance Criteria

- [ ] Typing `/dub bass` in the palette shows matching lines from notes containing "dub bass"
- [ ] Results display file path, line number, and the matching line with the term highlighted
- [ ] Clicking a result (or pressing Enter) opens the note
- [ ] Results update live as user types (with 300ms debounce)
- [ ] Typing without `/` prefix works exactly as before (file/metadata fuzzy search)
- [ ] Empty query after `/` shows hint text
- [ ] No results shows "No matches found"
- [ ] Arrow keys navigate results, Enter selects
- [ ] Works on mobile (375px viewport)
- [ ] Screenshots validate layout at desktop and mobile viewports
