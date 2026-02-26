# 2026-02-05: Epic Creation

## Discussion Summary

Continued discussion from `NEXT_SESSION.md` about implementing a memory system for Merlin, inspired by OpenClaw.

## Key Decisions Made

### Three-Layer Architecture

Refined the memory system into three distinct layers:

1. **User Memory** (`user.md`) — Facts about the user, preferences, always injected
2. **Daily Logs** (`logs/YYYY-MM-DD.md`) — Pre-compaction dumps, ephemeral context
3. **Knowledge Base** (`kb/*.md`) — Wiki-style, flat structure with links and tags

### Knowledge Base Format

After research into best practices, decided on:

- **Standard markdown** with YAML frontmatter (not wiki-style links)
- **Flat structure** (no subdirectories) — simpler queries
- **Links via** `[text](file.md)` — works everywhere
- **Tags via** `tags: [a, b]` in frontmatter — grep-friendly
- **Atomic notes** — one concept per file
- **Entry point** — `_index.md` as navigation root

### Interoperability

Key insight: the format should work with:
- **Obsidian** — for manual editing (disable wiki-links setting)
- **Nextcloud** — for syncing across devices
- **Claude/ripgrep** — for AI querying
- **Git** — for version control

This "plain text, future-proof" approach avoids lock-in.

## Research Sources

- [library-mcp](https://github.com/lethain/library-mcp) — Will Larson's MCP server
- [Markasten](https://github.com/andykuszyk/markasten) — Zettelkasten toolkit
- Various articles on AI-queryable knowledge bases

## Next Steps

Begin Phase 1: Create directory structure and templates.
