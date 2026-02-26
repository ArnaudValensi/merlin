# Session Viewer

Reference documentation for the session transcript viewer in the dashboard.

## Overview

The session viewer displays full Claude Code session transcripts as interactive timelines. It captures the complete conversation (user prompts, assistant responses, tool calls, and results) by recording `--output-format stream-json` output during invocations.

## Session Files

**Location**: `merlin-bot/logs/sessions/`

**Naming**: `{date}_{time}-{caller}-{session_id}.jsonl`

Example: `2026-02-06_20-21-45-discord-1c50b05a-18db-593d-8962-3b2101e5a3a4.jsonl`

Each file is a JSONL (one JSON object per line) capturing the stream-json output from a Claude Code invocation. A session may have multiple files (one per invocation/resume).

## Stream-JSON Event Types

Each line in the JSONL file has a `type` field:

| Type | Description |
|------|-------------|
| `system` | System configuration (model, tools available) |
| `assistant` | Claude's response (text or tool_use) |
| `user` | User message or tool_result |
| `result` | Final result (success/error, duration, cost, usage) |

### Assistant Message Content

```json
{
  "type": "assistant",
  "message": {
    "role": "assistant",
    "content": [
      {"type": "text", "text": "Here's what I found..."},
      {"type": "tool_use", "id": "toolu_...", "name": "Bash", "input": {"command": "ls"}}
    ]
  },
  "session_id": "abc..."
}
```

### Tool Result

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": [
      {"tool_use_id": "toolu_...", "type": "tool_result", "content": "file1.txt\nfile2.txt"}
    ]
  }
}
```

### Result (Final)

```json
{
  "type": "result",
  "subtype": "success",
  "duration_ms": 45188,
  "num_turns": 4,
  "total_cost_usd": 0.239,
  "session_id": "abc..."
}
```

## Dashboard Pages

### Session Detail (`/session/{filename}`)

Renders a timeline view of the session:

- **Assistant text** — rendered as markdown
- **Tool calls** — collapsible blocks showing tool name, input, and result
- **Metadata** — duration, cost, model, turn count from the `result` event
- **Navigation** — link back to logs page

### Session Links from Logs

The logs page (`/logs`) shows a "View session" link for each invocation event that has a `session_file` field. Clicking opens the session detail page.

## API Endpoints

### `GET /session/{filename}`

Renders the session viewer HTML page for the given JSONL file.

### `GET /api/session/{filename}`

Returns the raw JSONL content parsed as a JSON array of events.

## How Sessions Are Captured

In `claude_wrapper.py`, invocations use `--output-format stream-json`:

1. Claude Code outputs stream-json to stdout
2. `claude_wrapper.py` captures stdout line by line
3. Each JSON line is written to the session file
4. The session filename is recorded in the structured log (`session_file` field)

## Key Files

| File | Purpose |
|------|---------|
| `merlin_app.py` | Session viewer routes (`/session/{filename}`) |
| `claude_wrapper.py` | Captures stream-json output to session files |
| `structured_log.py` | Records `session_file` in invocation events |
| `templates/logs.html` | "View session" links in log table |
| `logs/sessions/*.jsonl` | Session transcript files |
