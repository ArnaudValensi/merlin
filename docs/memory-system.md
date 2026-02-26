# Memory System

Reference documentation for Merlin's 3-layer memory architecture: user profile, daily logs, and knowledge base.

## Overview

Merlin's memory lives in `merlin-bot/memory/` and is organized into three layers with increasing detail:

```
memory/
├── user.md                      # Layer 1: User profile (facts, preferences)
├── logs/                        # Layer 2: Daily conversation logs
│   ├── 2026-02-14.md
│   ├── 2026-02-15.md
│   └── ...
├── kb/                          # Layer 3: Knowledge base (Zettelkasten)
│   ├── _index.md                # Entry point with linked topics
│   ├── zettelkasten-method.md
│   ├── tqwt-tapered-quarter-wave-tube-theory.md
│   └── ...
├── media/                       # Uploaded images/files (from notes editor)
├── app-ideas-history.md         # Persistent history: generated app ideas
├── self-reflection-history.md   # Persistent history: daily self-reflection
└── digest-history.json          # Shared URLs from daily digests (JSON)
```

## Layer 1: User Profile (`user.md`)

A flat markdown file containing facts about the user — preferences, background, interests, gear, etc.

### Format

```markdown
# User Profile

## Basics
- Name: Alex
- Location: San Francisco, CA
- ...

## Interests
- Music: guitar, jazz, vinyl
- ...
```

### Management

- **Add facts**: `uv run remember.py --help` — appends user facts with dedup
- **Read**: Claude reads `user.md` as context via CLAUDE.md instructions
- **Edit**: Can be edited directly or via the notes editor dashboard

## Layer 2: Daily Logs (`logs/YYYY-MM-DD.md`)

One file per day capturing conversation highlights, decisions, and context.

### Format

```markdown
# Daily Log — 2026-02-14

## 02:33 — Pre-compaction memories

- User requested to save https://example.com to the knowledge base
- Discussed shader implementation for dissolve effect

---

## 14:20 — Pre-compaction (auto)

(No significant memories to save from session `abc123...`)

---
```

### When Entries Are Created

- **Pre-compaction**: Claude Code auto-saves before context window compression
- **Manual**: Claude creates entries when significant events happen

### Naming

Files are named `YYYY-MM-DD.md` using the current date.

## Layer 3: Knowledge Base (`kb/`)

A flat collection of interlinked markdown notes following Zettelkasten principles. Each note is atomic (one concept) with YAML frontmatter.

### Note Format

```yaml
---
title: Short Descriptive Title
created: 2026-02-05
tags: [tag1, tag2, tag3]
related: [other-note.md, another-note.md]
summary: One-line description of the note's content
---

# Title

Body content here. Can include links to other notes like
[Zettelkasten Method](zettelkasten-method.md).
```

### Frontmatter Fields

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Short descriptive title |
| `created` | date | `YYYY-MM-DD` creation date |
| `tags` | list | Categorization tags (see tag taxonomy below) |
| `related` | list | Filenames of related notes (bidirectional links) |
| `summary` | string | One-line description, useful at a glance |

### Index (`_index.md`)

The entry point to the knowledge base. Contains:
- Structure documentation (note format spec)
- Linked list of all topics with one-line descriptions
- Tag taxonomy with descriptions

### Tag Taxonomy

Tags are flat strings. Current categories include:
- **Music**: `piano`, `blues`, `reggae`, `synth`, `sound-design`, `dub`, `gear`, `mixing`
- **Gamedev**: `gamedev`, `pixel-art`, `shaders`, `glsl`, `tilemaps`, `animation`
- **Tech**: `python`, `ai`, `claude-code`, `agents`, `architecture`
- **Projects**: `merlin`, `startup`, `diy`, `active-build`
- **Knowledge**: `zettelkasten`, `knowledge-management`, `note-taking`

### Auto-Linking

When creating KB entries via `kb_add.py`, related notes are automatically discovered based on shared tags and suggested for cross-linking.

### File Naming

Kebab-case filenames: `tqwt-tapered-quarter-wave-tube-theory.md`

## Tools

### `memory_search.py` — Search Memory

Search across KB, logs, and user profile.

```bash
uv run memory_search.py --help
uv run memory_search.py "search query"
uv run memory_search.py --tags           # List all tags
uv run memory_search.py --tag piano      # Filter by tag
```

### `kb_add.py` — Add KB Entry

Create a new KB note with auto-linking to related notes.

```bash
uv run kb_add.py --help
uv run kb_add.py --title "Note Title" --tags tag1,tag2 --summary "Description"
```

- Generates kebab-case filename from title
- Discovers related notes by shared tags
- Updates `_index.md` with new entry
- Creates proper frontmatter

### `remember.py` — Add User Facts

Append facts to `user.md` with deduplication.

```bash
uv run remember.py --help
uv run remember.py "User prefers dark mode"
```

## Memory Injection

Memory is available to Claude through two mechanisms:

1. **CLAUDE.md directives**: `merlin-bot/CLAUDE.md` instructs Claude to read memory files when relevant.
2. **Session persistence**: Deterministic session IDs (UUID5 from thread/job ID) ensure Claude retains conversation context across interactions.

Memory is **not** injected into prompts directly — Claude reads the files on disk as needed via its tools.

## Persistent History Files

### `app-ideas-history.md`

Cumulative list of generated app ideas, organized by date and batch. Used by the `app-ideas` cron job to avoid duplicates.

### `self-reflection-history.md`

Daily proposals from the self-reflection cron job. Format:

```markdown
## YYYY-MM-DD
- **Proposed**: [one-line description]
- **Proposed**: [one-line description]
- **Context**: [what triggered the insight]
```

### `digest-history.json`

JSON array of URLs shared in daily digests, used for dedup:

```json
{"shared": [
  {"url": "https://...", "title": "...", "topic": "...", "date": "2026-02-09"}
]}
```

## Key Files

| File | Purpose |
|------|---------|
| `memory_search.py` | Search across all memory layers |
| `kb_add.py` | Create KB entries with auto-linking |
| `remember.py` | Add user facts to `user.md` |
| `memory/user.md` | User profile |
| `memory/logs/` | Daily conversation logs |
| `memory/kb/` | Knowledge base notes |
| `memory/kb/_index.md` | KB entry point and tag taxonomy |
