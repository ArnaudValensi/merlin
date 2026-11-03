---
name: merlin-notes
description: Search and manage Merlin's notes — knowledge base (typed OKF-style notes), daily logs, and user facts. Use this to recall past conversations, add knowledge, or look up stored information.
user-invocable: false
allowed-tools: Bash, Read
---

# Notes Skill

Merlin's notes system has three layers. The commands work from any directory. The notes directory is at `$(merlin config notes-dir)`.

The full format spec is `docs/dev/notes-system.md` in the Merlin repo; the KB's own `kb/conventions.md` holds the living type vocabulary. This skill is the operating card, not the spec.

## 1. Knowledge Base

Atomic typed notes in `<notes-dir>/kb/`: markdown + YAML frontmatter (`type` required; `title`, `description`, `tags`, `resource`, `created`, `updated`). Links live in the body as prose. The generated `kb/index.md` (title + description per note, grouped by type) is the catalog: read it first, nothing injects it for you.

### Reading protocol

1. **Read the index first**: `cat "$(merlin config notes-dir)/kb/index.md"`. For most questions it tells you which notes matter.
2. Narrow when needed: `merlin notes search kb --type company`, `--tag dub`, `--keyword "bass"` (combinable with none; one filter at a time).
3. Read the note: `cat "$(merlin config notes-dir)/kb/<file>.md"`, and follow its body links one hop when they are relevant.

### Writing protocol

1. **CEQRC first**: Capture the material, Explain it in your own words (never paste source content), Question what is unclear, Refine to one concept per note, Connect it to its real neighbors. If you cannot explain it without the original, you do not understand it enough to write it.
2. Check the vocabulary before coining: types and their counts are visible in the index; tags via `merlin notes search tags`; `kb/conventions.md` holds the living type vocabulary.
3. Create:

```bash
merlin kb add \
  --type technique \
  --title "Topic Name" \
  --tags "tag1, tag2" \
  --description "One-line description, specific and useful" \
  --resource "https://source-url-or-media-path" \
  --content "The note body..."

# Long content via stdin
cat notes.md | merlin kb add --type transcript --title "Talk" --tags "ai" --resource "https://youtube.com/..."
```

The command validates (duplicates, broken links, missing type), notices novel types/tags, and refreshes the index. **It never writes links.**

4. **Links are your job.** From the index, pick the 0-3 notes that genuinely relate and link them in the body with the relationship stated: inline (`Extends [X](x.md) with...`, `Contradicts [Y](y.md) because...`) or as an annotated bullet (`- [X](x.md) - what it adds`). A bare link is a lint finding. If the relation matters from the other side too, edit the neighbor and say so there.
5. Media: put the asset in `<notes-dir>/media/`, claim it via `resource:` (a list is allowed for several files), and make the body its textual projection: description for an image, transcript for audio/video, extraction for a PDF. Every media file must be owned by exactly one note.

### Maintenance

`merlin kb index` regenerates the index (safe anytime). `merlin kb check` is the conformance gate: run it after touching many notes; it must exit clean.

### When to add to the KB

Research findings worth keeping, decisions and their rationale, technical knowledge, company/project intel, reference material. Proactively suggest it when you discover something valuable: "This seems worth adding to the knowledge base — want me to save it?" In a job run there is no one to ask: save it directly and say what you added in your report. Before creating, scan the index: does a note on this already exist? Extend it rather than duplicating. Search the KB before researching; you may already have relevant knowledge.

## 2. Daily Logs

Day journal in `<notes-dir>/logs/YYYY-MM-DD.md` for anything noteworthy today (research findings, decisions, discoveries). Entries are `## HH:MM — <title>` blocks, appended directly with shell or Write.

```bash
# List all logs / search
merlin notes search log
merlin notes search log --keyword "error" --last 7
merlin notes search log --keyword "music" --from 2026-01-01 --to 2026-01-31
```

## 3. User Memory

`<notes-dir>/user.md`: durable facts about the user (preferences, identity, projects). Read it when you start working for them; update it when you learn something lasting, with `merlin remember`:

```bash
merlin remember add "Prefers dark mode in all editors"            # defaults to Notes section
merlin remember add "Name: Alex" --section identity
merlin remember add "Likes concise responses" --section preferences
merlin remember add "Working on the Merlin jobs" --section context
merlin remember list
```

**Rule of thumb:** short personal facts → `merlin remember add`; durable knowledge → `merlin kb add`; today's happenings → daily log.
