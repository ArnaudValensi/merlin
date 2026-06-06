---
name: notes
description: Search and manage Merlin's notes — knowledge base (Zettelkasten), daily logs, and user facts. Use this to recall past conversations, add knowledge, or look up stored information.
user-invocable: false
allowed-tools: Bash, Read
---

# Notes Skill

Merlin's notes system has three layers. Use the commands below to search and manage them. They work from any directory.

The notes directory is at `$(merlin config notes-dir)` (run this to get the resolved path).

## 1. Knowledge Base (Zettelkasten)

The KB is a network of atomic, interconnected markdown notes in `$(merlin config notes-dir)/kb/`.

### Search

```bash
# List all entries
merlin notes search kb

# Search by keyword
merlin notes search kb --keyword "docker"

# Search by tag
merlin notes search kb --tag "devops"
```

### Add a new entry

Use `merlin kb add` — it automatically finds related notes, links them, and adds backlinks:

```bash
# Add a note (auto-discovers related notes and links them)
merlin kb add \
  --title "Topic Name" \
  --tags "tag1, tag2" \
  --summary "One-line description" \
  --content "The actual content of the note..."

# Preview first (shows related notes without creating)
merlin kb add --title "Topic Name" --tags "tag1" --content "..." --dry-run

# Pipe long content via stdin
echo "Long content..." | merlin kb add --title "Topic" --tags "tag1"
```

The command handles:
- **Duplicate detection** — warns if a similar note exists
- **Related note discovery** — searches by tag overlap, title words, content keywords
- **Bidirectional linking** — links the new note to related notes AND adds backlinks
- **See Also section** — auto-generates a "See Also" with links to related notes

### KB Addition Protocol (CEQRC)

Before writing a KB note, run through this process:

**Capture** — Gather the raw material (web fetch, conversation, research). No special action needed.

**Explain** — Rephrase the knowledge in your own words. Don't paste source content. If you can't explain it without the original, you don't understand it enough to write a good note.

**Question** — What's unclear? What assumptions am I making? What does this NOT cover? If there's a meaningful gap, ask the user or research it before committing.

**Refine** — One concept per note. Cut anything that doesn't serve the core idea. Write a genuinely useful one-line summary, not a vague label. Aim for crisp notes — tight paragraphs, not brain dumps.

**Connect** — `merlin kb add` handles auto-linking, but also think about *how* top related notes connect: does the new note extend, support, or contradict them? Mention it in the content.

**When to go light:** If the user says "just save this" or it's a simple reference (URL, spec, config), still write a summary in your own words but skip Q and keep R minimal.

### When to add to the KB

- Research findings worth keeping long-term
- Decisions and their rationale
- Technical knowledge (setup guides, patterns, tools)
- Project notes, reference material
- Anything the user asks you to remember as knowledge

**Proactively suggest it** — if during research or conversation you discover something valuable, ask the user: "This seems worth adding to the knowledge base — want me to save it?"

## 2. Daily Logs

Day journal in `$(merlin config notes-dir)/logs/YYYY-MM-DD.md` — for anything noteworthy today.

### Search

```bash
# List all logs
merlin notes search log

# Search by keyword
merlin notes search log --keyword "deployment"

# Last N days
merlin notes search log --keyword "error" --last 7

# Date range
merlin notes search log --keyword "music" --from 2026-01-01 --to 2026-01-31
```

### What goes in daily logs

- Research findings, decisions, discoveries
- Interesting facts from conversations
- Anything worth remembering about the day

## 3. User Memory

`$(merlin config notes-dir)/user.md` — durable facts about the user. Always loaded automatically.

Use `merlin remember` to manage user facts:

```bash
# Add a fact (defaults to Notes section)
merlin remember add "Prefers dark mode in all editors"

# Add to a specific section
merlin remember add "Name: Alex" --section identity
merlin remember add "Likes concise responses" --section preferences
merlin remember add "Working on Merlin bot" --section context

# List all stored facts
merlin remember list
```

**Sections:** identity, preferences, context, notes

### What goes where

| If the user says... | Do this |
|---------------------|---------|
| "Remember that I prefer X" | `merlin remember add "Prefers X" --section preferences` |
| "My name is X" / "I'm in timezone X" | `merlin remember add "..." --section identity` |
| "I'm working on X" / "I'm interested in X" | `merlin remember add "..." --section context` |
| "Remember this fact about X" (general) | `merlin remember add "..."` (goes to notes) |
| "Save this research about X" (long/detailed) | `merlin kb add` (knowledge base entry) |

**Rule of thumb:** Short personal facts → `merlin remember add`. Longer knowledge → `merlin kb add`.

## Reading Full Entries

If search results look relevant but you need more detail, read the file directly:

```bash
NOTES=$(merlin config notes-dir)
cat "$NOTES/kb/some-topic.md"
cat "$NOTES/logs/2026-02-05.md"
```

## Tips

- **Search before researching** — the KB may already have relevant knowledge
- **Think about connections** — when adding a note, consider what it links to
- **Atomic notes** — one concept per file, well-linked to related notes
- **Use --dry-run** before adding to see what would be linked
- **Start broad** (keyword search), then narrow down by reading specific files
- Use `--discord` flag when sending results to Discord
