# Claude Code CLI Reference

Comprehensive reference for Claude Code CLI features relevant to the Merlin project.

## 1. CLI Flags and Options

### Core Execution Modes

| Flag | Mode | Usage |
|------|------|-------|
| `-p, --print` | Non-interactive, print and exit | `claude -p "query"` |
| `-c, --continue` | Resume most recent session | `claude --continue` |
| `-r, --resume <id>` | Resume specific session | `claude --resume "session-id"` |
| `--fork-session` | Create new session from old one | `claude --resume id --fork-session` |

### Session Management

| Flag | Purpose |
|------|---------|
| `--session-id <uuid>` | Use specific session UUID |
| `--no-session-persistence` | Don't save sessions (print mode only) |

### Output Formatting

| Flag | Options |
|------|---------|
| `--output-format` | `text` (default), `json`, `stream-json` |
| `--json-schema <schema>` | Validate structured output against schema |
| `--include-partial-messages` | Include streaming tokens in output |
| `--verbose` | Full turn-by-turn output |

Examples:
```bash
# JSON with metadata
claude -p "Summarize" --output-format json
# Output: {"session_id": "...", "result": "...", "usage": {...}, "model": "..."}

# Stream JSON for real-time responses
claude -p "Write a poem" --output-format stream-json --verbose --include-partial-messages

# Structured output with schema validation
claude -p "Extract function names" \
  --output-format json \
  --json-schema '{"type":"object","properties":{"functions":{"type":"array","items":{"type":"string"}}},"required":["functions"]}'

# Parse with jq
claude -p "Get summary" --output-format json | jq -r '.result'
```

### System Prompt Configuration

| Flag | Behavior |
|------|----------|
| `--system-prompt <text>` | **Replaces** entire default prompt (loses Claude Code features) |
| `--system-prompt-file <path>` | **Replaces** with file contents (print mode) |
| `--append-system-prompt <text>` | **Appends** to default prompt (keeps Claude Code behavior) |
| `--append-system-prompt-file <path>` | **Appends** file contents (print mode) |

**Key difference**: `--system-prompt` removes all Claude Code instructions. `--append-system-prompt` keeps default behavior + adds your instructions (recommended).

### Model Selection

| Flag | Values |
|------|--------|
| `--model <name>` | `sonnet`, `opus`, `haiku`, or full model ID |
| `--fallback-model <name>` | Auto-fallback if default is overloaded (print mode only) |

### Tool and Permission Control

| Flag | Purpose |
|------|---------|
| `--tools <list>` | Restrict available tools (`"default"`, `""`, or tool names) |
| `--allowedTools <rules>` | Auto-approve without prompting |
| `--disallowedTools <rules>` | Remove tools from context |
| `--permission-mode <mode>` | `default`, `plan`, `acceptEdits`, `dontAsk`, `bypassPermissions` |
| `--dangerously-skip-permissions` | Skip all permission prompts |

Permission rule syntax:
```
Bash                          # All bash commands
Bash(npm run *)              # Prefix match
Read(./src/**)               # Glob patterns
mcp__github__*              # All GitHub MCP tools
```

### Budget and Limits

| Flag | Purpose |
|------|---------|
| `--max-budget-usd <amount>` | Stop if cost exceeds limit (print mode) |
| `--max-turns <number>` | Max agentic iterations (print mode) |

### Custom Agents (CLI)

```bash
claude --agents '{
  "code-reviewer": {
    "description": "Expert code reviewer",
    "prompt": "You are a senior code reviewer...",
    "tools": ["Read", "Grep", "Glob", "Bash"],
    "model": "sonnet"
  }
}'

# Use single agent
claude --agent my-custom-agent "Do something"
```

### MCP Servers (CLI)

```bash
claude --mcp-config ./mcp.json
claude --mcp-config ./mcp.json ./project-mcp.json
claude --mcp-config '{"servers":{"api":{"type":"http","url":"..."}}}'
claude --strict-mcp-config --mcp-config ./approved.json
```

### Other Flags

| Flag | Purpose |
|------|---------|
| `--add-dir <dirs>` | Additional directories to allow tool access to |
| `--input-format <format>` | `text` (default), `stream-json` |
| `--debug <categories>` | Enable debug output |
| `--disable-slash-commands` | Disable all skills |

## 2. Skills (Slash Commands)

Skills extend Claude Code with custom commands. They're Markdown files with YAML frontmatter.

### File Structure

```
~/.claude/skills/<skill-name>/SKILL.md    # Personal
.claude/skills/<skill-name>/SKILL.md      # Project
```

Legacy format: `.claude/commands/my-command.md`

### Skill Template

```yaml
---
name: explain-code
description: Explains code with visual diagrams and analogies.
argument-hint: "[file-path]"
---

When explaining code, always include:
1. **Start with an analogy**
2. **Draw a diagram** using ASCII art
3. **Walk through code** step-by-step
```

### YAML Frontmatter Reference

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `name` | Yes | string | Skill identifier (lowercase, hyphens) |
| `description` | Recommended | string | When Claude should use it |
| `argument-hint` | No | string | Hint for autocomplete |
| `disable-model-invocation` | No | boolean | Only user can invoke |
| `user-invocable` | No | boolean | Hide from `/` menu if `false` |
| `allowed-tools` | No | string | Tools allowed without permission |
| `model` | No | string | `sonnet`, `opus`, `haiku`, `inherit` |
| `context` | No | string | `fork` to run in isolated subagent |
| `agent` | No | string | Which subagent for `context: fork` |

### String Substitutions

```yaml
---
name: fix-issue
---
Fix GitHub issue $ARGUMENTS
Session ID: ${CLAUDE_SESSION_ID}
Multiple args: $ARGUMENTS[0], $ARGUMENTS[1]
```

### Dynamic Context with Shell Commands

```yaml
---
name: pr-summary
---
## Pull request context
- Diff: !`gh pr diff`
- Comments: !`gh pr view --comments`

Summarize these changes...
```

Commands in backticks with `!` prefix execute before Claude sees them.

### Invocation Control

| Setting | User can invoke | Claude can invoke |
|---------|---|---|
| Default | Yes | Yes |
| `disable-model-invocation: true` | Yes | No |
| `user-invocable: false` | No | Yes |

## 3. Hooks System

### Hook Events

| Event | When | Matcher | Blockable |
|-------|------|---------|-----------|
| `SessionStart` | Session begins/resumes | `startup`, `resume`, `clear`, `compact` | No |
| `UserPromptSubmit` | User submits prompt | None | Yes |
| `PreToolUse` | Before tool executes | Tool name (regex) | Yes |
| `PostToolUse` | After tool succeeds | Tool name (regex) | No |
| `PostToolUseFailure` | After tool fails | Tool name (regex) | No |
| `Notification` | Notifications sent | Various | No |
| `SubagentStart` | Subagent spawns | Agent type name | No |
| `SubagentStop` | Subagent finishes | Agent type name | No |
| `Stop` | Claude finishes | None | Yes |
| `PreCompact` | Before compaction | `manual`, `auto` | No |
| `SessionEnd` | Session ends | Various | No |

### Hook Configuration

```json
{
  "hooks": {
    "EventName": [
      {
        "matcher": "regex-pattern",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/script.sh",
            "timeout": 600,
            "async": false
          }
        ]
      }
    ]
  }
}
```

### Hook Types

**Command** (shell script):
```json
{"type": "command", "command": "/path/to/script.sh"}
```
Script receives JSON on stdin. Exit 0 = allow, exit 2 = block (stderr sent to Claude).

**Prompt** (LLM-based):
```json
{"type": "prompt", "prompt": "Check if task is complete.", "model": "haiku"}
```

**Agent** (tool-using):
```json
{"type": "agent", "prompt": "Verify tests pass.", "model": "haiku"}
```

### Hook Input JSON (PreToolUse example)

```json
{
  "session_id": "abc123",
  "cwd": "/current/dir",
  "tool_name": "Bash",
  "tool_input": {"command": "npm test"},
  "tool_use_id": "toolu_01ABC..."
}
```

### Configuration Locations

| Location | Scope |
|----------|-------|
| `~/.claude/settings.json` | All projects |
| `.claude/settings.json` | Project (shared) |
| `.claude/settings.local.json` | Project (personal) |

## 4. MCP Servers

### Adding Servers

```bash
claude mcp add --transport http github https://api.githubcopilot.com/mcp/
claude mcp add --transport sse asana https://mcp.asana.com/sse
claude mcp add --transport stdio db -- npx -y @airtable/mcp-server
claude mcp list
claude mcp remove github
```

### Scopes

```bash
claude mcp add server-name https://...              # Local (default)
claude mcp add --scope project server-name https://... # Project (shared)
claude mcp add --scope user server-name https://...    # User (global)
```

### Config File Format (`.mcp.json`)

```json
{
  "mcpServers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/"
    },
    "database": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@package/mcp"],
      "env": {"DB_URL": "postgresql://..."}
    }
  }
}
```

## 5. Settings and Configuration

### Precedence (highest to lowest)

1. Managed settings (system-wide)
2. Command-line arguments
3. Local project settings (`.claude/settings.local.json`)
4. Project settings (`.claude/settings.json`)
5. User settings (`~/.claude/settings.json`)

### Settings Template

```json
{
  "permissions": {
    "allow": ["Bash(npm run *)", "Read(~/.zshrc)"],
    "deny": ["Bash(curl *)", "Read(.env)"]
  },
  "hooks": {},
  "env": {"CUSTOM_VAR": "value"},
  "model": "claude-opus-4-5-20251101"
}
```

### Permission Evaluation Order

Deny → Ask → Allow (first match wins)

## 6. Custom Agents (File-based)

### File Structure

```
~/.claude/agents/<name>/agent.md    # User-level
.claude/agents/<name>/agent.md      # Project-level
```

### Agent Template

```yaml
---
name: code-reviewer
description: Expert code reviewer.
tools: Read, Grep, Glob, Bash
model: sonnet
permissionMode: default
---

You are a senior code reviewer. Analyze code for quality and security.
```

### Frontmatter Fields

| Field | Values | Purpose |
|-------|--------|---------|
| `name` | lowercase, hyphens | Unique agent ID |
| `description` | text | When Claude delegates to agent |
| `tools` | Tool list | Restrict capabilities |
| `model` | `sonnet`, `opus`, `haiku`, `inherit` | Which model |
| `permissionMode` | `default`, `acceptEdits`, `dontAsk`, `bypassPermissions`, `plan` | Permission handling |

### Built-in Agents

| Name | Model | Tools | Purpose |
|------|-------|-------|---------|
| Explore | Haiku | Read-only | Fast codebase exploration |
| Plan | Inherits | Read-only | Research for planning |
| general-purpose | Inherits | All | Complex multi-step tasks |
| Bash | Inherits | Bash | Terminal operations |

## 7. Output Format Details

### JSON Output

```bash
claude -p "query" --output-format json
```

```json
{
  "session_id": "abc123def456",
  "result": "The actual text response",
  "usage": {
    "input_tokens": 1234,
    "output_tokens": 567
  },
  "model": "claude-sonnet-4-5-20250929",
  "stop_reason": "end_turn"
}
```

### Structured Output

```bash
claude -p "Extract data" --output-format json --json-schema '{"type":"object",...}'
```

```json
{
  "session_id": "...",
  "structured_output": {"functions": ["login", "logout"]},
  "usage": {...}
}
```

### Stream JSON

```bash
claude -p "query" --output-format stream-json --verbose --include-partial-messages
```

One JSON object per line with `type` field for event type.

## 8. Session Management

### Storage

```
~/.claude/projects/<project>/<session_id>/
├── transcript.jsonl
└── subagents/
```

### Context-Preserving Continuation

```bash
# Get session ID from first call
session_id=$(claude -p "Plan auth refactor" --output-format json | jq -r '.session_id')

# Follow-up with preserved context
claude -p "Implement login endpoint" --resume "$session_id"

# Final step
claude -p "Add tests" --resume "$session_id"
```

### Cleanup

Sessions auto-cleanup based on `cleanupPeriodDays` setting (default: 30 days).

## 9. Practical Patterns

### Automated Pipeline

```bash
claude -p "Fix failures" \
  --allowedTools "Bash,Read,Edit" \
  --max-turns 5 \
  --max-budget-usd 2.50 \
  --output-format json
```

### Code Review from PR

```bash
gh pr diff | claude -p \
  --append-system-prompt "Review for security and performance" \
  --output-format json | jq -r '.result'
```

### Unattended Execution (Cron)

```bash
claude -p --dangerously-skip-permissions "do task" \
  --resume "$SESSION_ID" \
  --output-format json
```
