# stream-json Samples

Real output from `claude -p --output-format stream-json --verbose --dangerously-skip-permissions`.

## Simple text-only response

Command: `claude -p --output-format stream-json --verbose --dangerously-skip-permissions --max-turns 1 "What is 2+2?"`

3 events: system → assistant(text) → result

```jsonl
{"type":"system","subtype":"init","cwd":"/home/user/merlin/merlin-bot","session_id":"0cea7aee-7102-431f-aea5-69f19e031e1e","tools":["Task","TaskOutput","Bash","Glob","Grep","ExitPlanMode","Read","Edit","Write","NotebookEdit","WebFetch","TodoWrite","WebSearch","TaskStop","AskUserQuestion","Skill","EnterPlanMode","ToolSearch"],"mcp_servers":[],"model":"claude-opus-4-6","permissionMode":"bypassPermissions","claude_code_version":"2.1.34","agents":["Bash","general-purpose","statusline-setup","Explore","Plan","claude-code-guide"],"skills":["keybindings-help","debug","discord","dashboard","cron","memory","self-awareness","screenshot"],"uuid":"569698a5-f39d-4c1a-8617-f7b3e278a355"}
{"type":"assistant","message":{"model":"claude-opus-4-6","id":"msg_01Yc5iCyozcP1MvFdpyx2aHq","type":"message","role":"assistant","content":[{"type":"text","text":"4."}],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":3,"cache_creation_input_tokens":11421,"cache_read_input_tokens":13930,"output_tokens":1}},"parent_tool_use_id":null,"session_id":"0cea7aee-7102-431f-aea5-69f19e031e1e"}
{"type":"result","subtype":"success","is_error":false,"duration_ms":2101,"duration_api_ms":2017,"num_turns":1,"result":"4.","session_id":"0cea7aee-7102-431f-aea5-69f19e031e1e","total_cost_usd":0.07851125,"usage":{"input_tokens":3,"cache_creation_input_tokens":11421,"cache_read_input_tokens":13930,"output_tokens":6}}
```

## Multi-turn with tool use

Command: `claude -p --output-format stream-json --verbose --dangerously-skip-permissions --max-turns 3 "What is 2+2? Then read the file merlin.py and tell me how many lines it has."`

6 events: system → assistant(text) → assistant(tool_use) → user(tool_result) → assistant(text) → result

```jsonl
{"type":"system","subtype":"init","cwd":"/home/user/merlin/merlin-bot","session_id":"efdbade3-f62f-4569-88c2-16c1b29e3fbc","model":"claude-opus-4-6","permissionMode":"bypassPermissions","claude_code_version":"2.1.34"}
{"type":"assistant","message":{"model":"claude-opus-4-6","id":"msg_01RAKtwdPihxR1NtS5RjRDPf","content":[{"type":"text","text":"\n\n2+2 = 4."}],"usage":{"input_tokens":3,"cache_creation_input_tokens":6269,"cache_read_input_tokens":19099,"output_tokens":2}},"parent_tool_use_id":null,"session_id":"efdbade3-f62f-4569-88c2-16c1b29e3fbc"}
{"type":"assistant","message":{"model":"claude-opus-4-6","id":"msg_01RAKtwdPihxR1NtS5RjRDPf","content":[{"type":"tool_use","id":"toolu_01S54tom7kgSpeeFYjQJMxkd","name":"Read","input":{"file_path":"/home/user/merlin/merlin-bot/merlin.py"}}],"usage":{"input_tokens":3,"cache_creation_input_tokens":6269,"cache_read_input_tokens":19099,"output_tokens":2}},"parent_tool_use_id":null,"session_id":"efdbade3-f62f-4569-88c2-16c1b29e3fbc"}
{"type":"user","message":{"role":"user","content":[{"tool_use_id":"toolu_01S54tom7kgSpeeFYjQJMxkd","type":"tool_result","content":"     1→# /// script\n...399 lines..."}]},"parent_tool_use_id":null,"session_id":"efdbade3-f62f-4569-88c2-16c1b29e3fbc","tool_use_result":{"type":"text","file":{"filePath":"/home/user/merlin/merlin-bot/merlin.py","numLines":399,"startLine":1,"totalLines":399}}}
{"type":"assistant","message":{"model":"claude-opus-4-6","id":"msg_01VKqzRVgsDAGAAsUpdvvKrG","content":[{"type":"text","text":"`merlin.py` has **399 lines**."}],"usage":{"input_tokens":1,"cache_creation_input_tokens":5399,"cache_read_input_tokens":25368,"output_tokens":1}},"parent_tool_use_id":null,"session_id":"efdbade3-f62f-4569-88c2-16c1b29e3fbc"}
{"type":"result","subtype":"success","is_error":false,"duration_ms":5213,"duration_api_ms":5124,"num_turns":2,"result":"`merlin.py` has **399 lines**.","session_id":"efdbade3-f62f-4569-88c2-16c1b29e3fbc","total_cost_usd":0.0975285,"usage":{"input_tokens":4,"cache_creation_input_tokens":11668,"cache_read_input_tokens":44467,"output_tokens":94}}
```

## Key observations

1. **`--verbose` is required** — without it, `stream-json` errors: "When using --print, --output-format=stream-json requires --verbose"

2. **Text + tool_use in same turn** → emitted as **separate** `assistant` events with the **same `message.id`**. The text comes first, then the tool_use.

3. **Tool results** have both `message.content[].content` (raw text the model sees) and `tool_use_result` (structured metadata, e.g., file info for Read).

4. **`result` event** contains aggregated usage across all API calls, `total_cost_usd`, `num_turns`, and the final `result` text.

5. **`parent_tool_use_id`** is null for top-level events. Would be non-null for subagent tool calls (Task tool).

6. **Token usage per turn** is in `assistant.message.usage` for each individual API call. Aggregated totals are in the `result` event.
