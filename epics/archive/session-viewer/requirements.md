# Session Viewer — Requirements

## Goal

Add a dedicated page to the monitoring dashboard for viewing the full Claude session transcript of any invocation — every step Claude took, tool calls, results, and responses — rendered like an interactive mode conversation.

## Context

Currently we run `claude -p --output-format json` which only captures the final result. The full step-by-step (tool calls, reasoning, intermediate responses) is lost.

Claude Code CLI supports `--output-format stream-json --verbose` which streams NDJSON events for every step:

| Event type | Description |
|------------|-------------|
| `system` (subtype: `init`) | First event. Session ID, model, tools, version |
| `assistant` | Claude's text responses and tool calls (`content[]` with `type: "text"` or `type: "tool_use"`) |
| `user` | Tool results (`content[]` with `type: "tool_result"`, plus `tool_use_result` metadata) |
| `result` | Final event. Duration, cost, total usage, num_turns |

Example flow for a tool-using session:
```
system/init → assistant(text: "2+2 = 4") → assistant(tool_use: Read merlin.py) → user(tool_result: file content) → assistant(text: "399 lines") → result/success
```

Note: `stream-json` requires the `--verbose` flag.

## User Flow

1. User is on the Logs page
2. Clicks "View session" link on an invocation row
3. Opens `/session/<session-file>` page
4. Sees the full conversation timeline: Claude's text, tool calls with inputs, tool results, follow-up responses

## Requirements

### 1. Switch to stream-json output in claude_wrapper.py

Change `invoke_claude()` to use `--output-format stream-json --verbose` instead of `--output-format json`.

- Save the full NDJSON stream to `logs/sessions/<timestamp>-<caller>-<session>.jsonl`
- Parse the final `result` event line for existing structured log fields (exit_code, duration, usage, etc.)
- Add `session_file` field to invocation events in `structured.jsonl` (just the filename, not full path)
- Maintain backward compatibility: `ClaudeResult` fields stay the same, parsed from the `result` event

### 2. API endpoint

`GET /api/session/<filename>` — reads the session JSONL file, returns the parsed events as a JSON array.

- Validate filename (no path traversal)
- Return 404 if file doesn't exist
- Each event is a JSON object as stored in the JSONL

### 3. Session page (`/session/<filename>`)

A new page rendering the conversation as a timeline:

- **Header bar**: caller, session ID, timestamp, duration, exit code, model
- **Conversation timeline**: each event rendered in order:
  - **Assistant text**: Claude's responses, rendered as markdown or preformatted
  - **Tool call**: tool name + input parameters, styled as a collapsible card
  - **Tool result**: output/content, collapsible (collapsed by default for large results like file reads)
  - **System init**: collapsed metadata section (model, tools, version)
  - **Result summary**: cost, total tokens, num_turns
- **Usage sidebar or footer**: token counts (in/out/cache), cost, model breakdown

### 4. Link from Logs page

Invocation rows in the Logs page get a "View session" link that navigates to `/session/<session_file>`.

- Only shown when `session_file` field exists in the event
- Opens in same tab with a back link to return to Logs

### 5. Design

- Follow existing dashboard conventions (dark theme, same CSS variables)
- Read `docs/dashboard-architecture.md` for patterns
- Conversation timeline should feel like reading an interactive Claude Code session
- Tool calls visually distinct from text responses (different background, icon/badge)
- Large tool results (file reads, long outputs) collapsed by default with "Show full output" toggle
- Back link to return to the Logs page
- Validate with screenshots

## Technical Notes

### stream-json event structure

**assistant event with text:**
```json
{
  "type": "assistant",
  "message": {
    "content": [{"type": "text", "text": "The answer is 4."}],
    "usage": {"input_tokens": 3, "output_tokens": 10}
  }
}
```

**assistant event with tool call:**
```json
{
  "type": "assistant",
  "message": {
    "content": [{"type": "tool_use", "id": "toolu_xxx", "name": "Read", "input": {"file_path": "/path/to/file"}}]
  }
}
```

**user event (tool result):**
```json
{
  "type": "user",
  "message": {
    "content": [{"tool_use_id": "toolu_xxx", "type": "tool_result", "content": "file contents..."}]
  },
  "tool_use_result": {"type": "text", "file": {"filePath": "...", "numLines": 399}}
}
```

**Note:** When an assistant turn contains both text AND a tool call, they are emitted as separate `assistant` events with the same `message.id`.

### Storage

- Session files: `logs/sessions/<timestamp>-<caller>-<session>.jsonl` (one NDJSON line per event)
- Gitignored (same as existing logs)
- No rotation for now (same policy as other logs)

## Non-goals

- No token-level streaming (`--include-partial-messages`) — we only need completed events
- No real-time streaming of active sessions
- No editing or replaying sessions
- No session list/browsing page (access is always via the Logs page link)
