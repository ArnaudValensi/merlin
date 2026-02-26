# Epic: Memory System for Merlin

## Overview

Implement a three-layer memory system for Merlin, enabling persistent knowledge across sessions, user personalization, and a searchable knowledge base.

## Goals

1. **Continuity** — Remember past conversations and context
2. **Personalization** — Store user preferences and context
3. **Knowledge Base** — Maintain a Zettelkasten-style knowledge network where connections between ideas emerge over time
4. **Context Preservation** — Save important info before session compaction
5. **Active Curation** — Merlin should proactively recognize valuable information and suggest adding it to the KB
6. **Emergent Knowledge** — The interconnected web of notes should enable discovery of new connections and ideas across topics

## Architecture

### Three Layers

| Layer | Location | Purpose | Injection |
|-------|----------|---------|-----------|
| **User Memory** | `memory/user.md` | Facts about the user | Always (system prompt) |
| **Daily Logs** | `memory/logs/YYYY-MM-DD.md` | Day journal: research findings, decisions, discoveries, compaction dumps — anything noteworthy | On demand (search) |
| **Knowledge Base** | `memory/kb/*.md` | Zettelkasten: atomic notes interconnected via tags and links, forming a knowledge web | On demand (search) |

### Directory Structure

```
merlin-bot/memory/
├── user.md                    # User facts (always injected)
├── logs/
│   └── 2026-02-05.md          # Daily logs (append-only)
└── kb/
    ├── _index.md              # Root entry point
    ├── topic-name.md          # Knowledge entries (flat)
    └── ...
```

## Knowledge Base Format

### File Structure

Standard markdown with YAML frontmatter:

```yaml
---
title: Short Descriptive Title
created: 2026-02-05
tags: [tag1, tag2]
related: [other-file.md, another.md]
summary: One-line description for quick scanning
---

# Title

Content here...

## See Also
- [Related Topic](related-file.md)
```

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Format | Markdown + YAML frontmatter | Universal, parseable, AI-friendly |
| Structure | Flat (no subdirs) | Simple queries, fast grep |
| Links | Standard markdown `[text](file.md)` | Works everywhere (Obsidian, GitHub, etc.) |
| Tags | YAML array: `tags: [a, b]` | Grep-friendly, Obsidian-compatible |
| Naming | Descriptive slugs: `topic-subtopic.md` | Human-readable, unique |
| Entry points | `_index.md` | Navigation starting point |
| Note size | Atomic (one concept per file) | Focused search results |

### Zettelkasten Philosophy

The KB follows the Zettelkasten method — a network of atomic notes where the value comes from **connections**, not individual entries.

**Core principles:**
- **Atomic notes** — One concept per file. If a note covers two distinct ideas, split it.
- **Links are first-class** — Every note should link to related notes via `[text](file.md)` and `related:` frontmatter. The web of links *is* the knowledge.
- **Tags group themes** — Tags provide a second axis of navigation alongside links. A note about a mechanical keyboard might be tagged `[tech, gear, shopping]`.
- **Emergent connections** — When studying different subjects, unexpected links appear between them. The structure should make these visible.
- **Knowledge compounds** — As the KB grows, existing notes gain value from new connections. A note written months ago might suddenly relate to today's research.

**Future vision:** A periodic job where Merlin reviews the KB graph and surfaces new connections, synthesis, or ideas that emerge from the accumulated knowledge.

### Merlin's Role as Knowledge Curator

Merlin must be **self-aware** about the memory system. It's not just a passive storage layer — it's an active work method:

- **Recognize valuable information** during research, conversations, and cron jobs
- **Proactively suggest** adding things to the KB: "This seems worth saving — want me to add it?"
- **Think about connections** when creating notes — what existing notes relate? What tags apply?
- **Search before researching** — the KB may already contain relevant knowledge
- **Maintain quality** — well-linked, atomic notes with proper frontmatter

### Interoperability

The knowledge base is designed to work with:
- **Obsidian** — Manual editing (disable wiki-links in settings)
- **Nextcloud** — Sync across devices
- **Git** — Version control (optional)
- **Any text editor** — It's just markdown

## Components

### 1. Memory Injection

Modify `claude_wrapper.py` to inject memory into Claude's context:
- Always inject `user.md` via `--append-system-prompt`
- Optionally inject relevant KB entries based on context

### 2. Daily Logs & Pre-Compaction Hook

Daily logs (`logs/YYYY-MM-DD.md`) capture anything noteworthy from the day:
- Research findings, decisions made, interesting discoveries
- Pre-compaction context dumps (via `PreCompact` hook)
- Append-only (multiple entries per day)

### 3. Search Tools

Subagent-based search (keeps parent context clean):
- **kb_search** — Search knowledge base by keyword, tag, or content
- **log_search** — Search daily logs by date range or keyword
- Uses ripgrep, find, glob

### 4. Memory Management

Skills or tools for memory operations:
- **remember** — Add to user.md or create KB entry
- **kb_add** — Create new knowledge base entry
- **kb_list** — List entries by tag or recent

## Acceptance Criteria

### Must Have
- [ ] `memory/` directory structure created
- [ ] `user.md` template with example content
- [ ] `_index.md` as KB entry point
- [ ] `claude_wrapper.py` injects `user.md` into system prompt
- [ ] `PreCompact` hook saves to daily log
- [ ] At least one search tool (kb_search) working

### Should Have
- [ ] Frontmatter validation (warn on missing fields)
- [ ] Tag index generation (`_tags.md`)
- [ ] Multiple search modes (by tag, by content, by date)

### Nice to Have
- [ ] Obsidian configuration guide
- [ ] Nextcloud sync setup guide
- [ ] Auto-summarization of daily logs
- [ ] Periodic KB review job — Merlin traverses the knowledge graph and surfaces new connections or ideas
- [ ] Vector search (future consideration)

## Open Questions

1. **How much of user.md to inject?** Full file or summary?
2. **Should KB entries be auto-loaded based on conversation topic?**
3. **How to handle conflicts if edited externally while Claude is running?**

## References

- [library-mcp](https://github.com/lethain/library-mcp) — Will Larson's MCP server for markdown KB
- [Markasten](https://github.com/andykuszyk/markasten) — Zettelkasten toolkit
- OpenClaw memory architecture (see `openclaw/` submodule)
- [Zettelkasten](https://zettelkasten.de/overview/) — The note-taking method inspiring the KB design
