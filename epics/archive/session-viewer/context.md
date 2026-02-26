# Session Viewer — Implementation Context

Everything needed to start implementing this epic from a fresh session.

## What to read first

1. **This file** — overview and pointers
2. **`requirements.md`** — full requirements and design
3. **`stream-json-samples.md`** — annotated real output from stream-json
4. **`sample-simple.jsonl`** — raw 3-event session (text only)
5. **`sample-tool-use.jsonl`** — raw 6-event session (with Read tool call)

## Key files to modify

### claude_wrapper.py (`merlin-bot/claude_wrapper.py`)

Currently uses `--output-format json`. Needs to switch to `--output-format stream-json --verbose`.

Changes needed:
- Add `--verbose` to the command
- Change `--output-format json` to `--output-format stream-json`
- Capture stdout as NDJSON lines (not a single JSON blob)
- Save the full stream to `logs/sessions/<timestamp>-<caller>-<session>.jsonl`
- Parse the last line (type: `result`) for the fields currently extracted from JSON output: `result`, `session_id`, `usage`, `model`, `num_turns`, `exit_code`
- Add `session_file` (just the filename) to the `log_event()` call
- `ClaudeResult` dataclass stays the same — fields are populated from the `result` event

### dashboard.py (`merlin-bot/dashboard.py`)

Add:
- `GET /api/session/<filename>` endpoint — reads JSONL, returns events as JSON array
- `GET /session/<filename>` route — renders session template
- Filename validation (prevent path traversal)

### New template: `merlin-bot/templates/session.html`

Extends `base.html`. Renders conversation timeline from JSONL events.

Event rendering:
- `system/init` → collapsed metadata header (model, version, tools)
- `assistant` with `content[].type == "text"` → Claude's response in a styled block
- `assistant` with `content[].type == "tool_use"` → tool call card (name, input params)
- `user` with `content[].type == "tool_result"` → tool result (collapsed for large content)
- `result` → summary footer (duration, cost, tokens)

### logs.html (`merlin-bot/templates/logs.html`)

Add "View session" link to invocation rows when `session_file` field is present.

### dashboard.css (`merlin-bot/static/dashboard.css`)

Add styles for:
- Conversation timeline layout
- Assistant text blocks
- Tool call cards
- Tool result blocks (collapsible)
- Session header bar

## Architecture reference

Read `docs/dashboard-architecture.md` for:
- CSS custom properties (theme colors)
- JS module patterns (API, Refresh)
- How to add a new page
- Chart.js patterns (not needed here but good context)

## Storage

- Session JSONL files go in `logs/sessions/` (gitignored via existing `logs/` rule)
- Filename format: `<timestamp>-<caller>-<session>.jsonl`
- The old `logs/claude/` per-invocation text logs can coexist (or be deprecated later)

## Testing

- Unit tests for JSONL parsing (new session file format)
- Unit tests for the result event parser (extracts ClaudeResult fields)
- Test the API endpoint (filename validation, 404 handling)
- Visual validation with screenshots (screenshot skill)

## Current state of the codebase

- 275 tests passing
- Dashboard runs on port 3123 with basic auth
- 3 existing pages: Overview, Performance, Logs
- `structured.jsonl` has event types: invocation, bot_event, cron_dispatch
- Invocation events currently have: caller, prompt, duration, exit_code, num_turns, tokens_in, tokens_out, session_id, model
