# Tasks: Memory System

## Status Legend
- [ ] Todo
- [x] Done
- [~] In Progress
- [-] Blocked

---

## Phase 1: Foundation

### 1.1 Create directory structure
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: None

Create the memory directory structure:
```
merlin-bot/memory/
├── user.md
├── logs/
└── kb/
    └── _index.md
```

### 1.2 Create user.md template
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 1.1

Initial user memory file with sections for preferences, context, etc.

### 1.3 Create KB frontmatter template
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 1.1

Document the standard frontmatter format and create `_index.md`.

### 1.4 Modify claude_wrapper.py for memory injection
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 1.2

Added `_load_user_memory()` to `claude_wrapper.py` — automatically injects `user.md`
into every Claude call. Single entry point means all callers get memory for free.

---

## Phase 2: Pre-Compaction Hook

### 2.1 Research Claude Code hook configuration
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: None

Researched hooks via claude-code-guide agent. PreCompact receives JSON on stdin
with session_id, transcript_path, trigger. Can use command/prompt/agent types.

### 2.2 Implement PreCompact hook script
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 2.1

Created `.claude/hooks/pre-compact-memory.py` — logs compaction events to daily log.
Registered in `.claude/settings.json` with matcher "*" (both manual and auto).

### 2.3 Create daily log format
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 1.1

Format: `memory/logs/YYYY-MM-DD.md` with timestamped entries.
Each entry includes session ID, transcript path, and compaction type.

### 2.4 Test pre-compaction flow
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 2.2, 2.3

Tested hook script manually — creates log entry correctly.

---

## Phase 3: Search Tools

### 3.1 Implement kb_search tool/skill
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 1.3

Created `memory_search.py` with `kb` subcommand — searches KB by keyword (ripgrep)
or tag (frontmatter parsing). Lists all entries when no filters given.
19 pytest tests in `tests/test_memory_search.py`.

### 3.2 Implement log_search tool/skill
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 2.3

Created `memory_search.py` with `log` subcommand — searches daily logs by keyword,
date range (`--from`/`--to`), or relative (`--last N` days). Lists log files when
no keyword given.

### 3.3 Add search to Claude's available tools
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 3.1, 3.2

Registered as `.claude/skills/memory/SKILL.md` — follows existing discord/cron
skill pattern. Tells Claude how to use `memory_search.py` for all search operations.

---

## Phase 4: Memory Management

### 4.1 Implement "remember" skill
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 1.2

Created `remember.py` — adds facts to `user.md` with section detection:
- Sections: identity, preferences, context, notes
- Auto-removes placeholder text on first use
- Appends to existing facts, auto-adds bullet prefix
- `list` subcommand shows all stored facts
- 18 pytest tests in `tests/test_remember.py`
- Memory skill updated with routing guidance (short facts → remember.py, knowledge → kb_add.py)

### 4.2 Implement kb_add skill
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 1.3

Created `kb_add.py` with automatic Zettelkasten link discovery:
- Duplicate detection (title/filename match)
- Related note discovery (tag overlap, title words, content keywords)
- Bidirectional linking (new note links to related + backlinks added)
- See Also section auto-generated
- `--dry-run` mode for preview
- 29 pytest tests in `tests/test_kb_add.py`

### 4.3 Implement kb_list skill
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 3.1

Already covered by `memory_search.py kb` (lists all entries, filter by tag).
Documented in memory skill SKILL.md.

---

## Phase 5: Polish & Documentation

### 5.1 Obsidian compatibility guide
- **Status**: [-]
- **Assignee**: —
- **Dependencies**: 1.3

Skipped — not needed for now.

### 5.2 Tag index generation
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: 3.1

Added `tags` subcommand to `memory_search.py` — lists all tags with entry counts
and file lists. 3 new tests.

### 5.3 Validation & testing
- **Status**: [x]
- **Assignee**: Claude
- **Dependencies**: All above

Created `tests/test_memory_e2e.py` with 6 end-to-end tests:
- KB add → search finds it (keyword + tag)
- Two related entries → bidirectional links created
- Remember facts → list shows them
- Log search finds entries
- Tags reflect KB entries
- Duplicate detection blocks creation

---

## Notes

- Phases can overlap; 1 and 2 are parallelizable
- Phase 3 depends on having content to search
- Vector search deferred to future epic if needed
