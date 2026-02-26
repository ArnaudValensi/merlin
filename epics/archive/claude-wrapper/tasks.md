# Claude Wrapper — Tasks

## T1: Create basic wrapper script with subprocess invocation
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: —
- **Description**: Create `merlin-bot/claude_wrapper.py` with PEP 723 inline dependencies. Implement core `invoke_claude()` function that:
  - Runs `claude -p` as a subprocess
  - Passes the user's prompt
  - Supports `--resume <session_id>` for conversation continuity
  - Uses `--output-format json` to get structured output (session_id, result, usage)
  - Always passes `--dangerously-skip-permissions` (wrapper is always unattended)
  - Uses `--append-system-prompt` (not `--system-prompt`) to keep Claude Code's built-in tools working
  - Captures stdout and stderr separately
  - Returns a structured result (output, stderr, exit code, session_id, duration)

## T2: Implement logging
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T1
- **Description**: Add logging to the wrapper. On every invocation:
  - Create `merlin-bot/logs/claude/` directory if it doesn't exist
  - Write one log file per invocation: `<YYYY-MM-DD_HH-MM-SS>-<caller>-<session-id>.log`
  - Log contents: caller, start/end timestamps, prompt, session ID, full stdout, full stderr, exit code, duration
  - Use Python `logging` module for the wrapper's own operational logs

## T3: Add CLI interface
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T1
- **Description**: Add `argparse` CLI so the wrapper can be called standalone:
  ```
  uv run claude_wrapper.py --caller <name> --session <id> "prompt"
  ```
  Optional flags:
  - `--model <model>` (default: inherit from Claude Code)
  - `--allowed-tools <tools>` (pass through to `--allowedTools`)
  - `--append-system-prompt <text>` (additional instructions)
  - `--no-skip-permissions` (opt out of the default `--dangerously-skip-permissions` for testing)
  - `--max-turns <n>` (limit agentic iterations)
  - `--max-budget-usd <amount>` (cost limit)
  Print the result to stdout so cron scripts can capture it.

## T4: Add Python API (importable function)
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T1
- **Description**: Ensure `invoke_claude()` is importable from other Python scripts (e.g. `merlin.py`). The function signature should accept:
  - `prompt: str`
  - `caller: str`
  - `session_id: str | None`
  - `model: str | None`
  - `allowed_tools: list[str] | None`
  - `append_system_prompt: str | None`
  - `skip_permissions: bool = True` (default on, opt out for testing)
  - `max_turns: int | None`
  - `max_budget_usd: float | None`
  Returns a dataclass/dict with: `result`, `session_id`, `stderr`, `exit_code`, `duration`, `usage`.
  Same logging behavior whether called via CLI or Python import.

## T5: Error handling
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T1, T2
- **Description**: Handle failure cases gracefully:
  - Non-zero exit code from Claude: log error, return it to caller (don't crash)
  - Claude binary not found: clear error message
  - Timeout: support a configurable timeout, log if exceeded
  - JSON parse failure (if Claude output isn't valid JSON): fall back to raw text, log warning
  - Log directory not writable: warn but don't crash

## T6: Unit tests (pytest)
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T1, T2, T3, T4, T5
- **Description**: Write pytest unit tests for the wrapper. Tests should mock the `claude` subprocess to avoid real API calls. Cover:
  - `invoke_claude()` builds the correct command with all flags
  - `--dangerously-skip-permissions` is always included
  - `--output-format json` is always included
  - Session ID is passed through correctly
  - Structured result is parsed from JSON output (session_id, result, usage)
  - Logging: verify log file is created with correct naming and content
  - Error handling: non-zero exit code, invalid JSON output, missing claude binary
  - CLI interface: argparse parses all flags correctly
  - Python API and CLI produce identical logging

## T7: End-to-end validation
- **Status**: `done`
- **Assignee**: claude
- **Dependencies**: T6
- **Description**: Live end-to-end test against real Claude Code (not mocked):
  - Call wrapper from CLI with a simple prompt, verify log file is created with correct content
  - Call wrapper from CLI with `--session` to test session resumption
  - Import and call `invoke_claude()` from Python, verify same logging
  - Test error case: bad session ID, verify error is logged and returned
  - Verify log file naming convention is correct
  - Document results in journal entry
