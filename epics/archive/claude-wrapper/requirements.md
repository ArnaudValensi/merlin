# Claude Wrapper — Requirements

## Goal

A single Python script (`claude-wrapper.py`) that is the only way to invoke Claude Code in the Merlin project. Both the Discord bot and cron jobs call this wrapper — never `claude` directly.

## Requirements

### R1: Invoke Claude Code — `accepted`
- Run `claude -p` as a subprocess
- Pass through the user's prompt
- Support `--resume <session_id>` for conversation continuity
- Support other Claude Code flags as needed (model, allowed tools, system prompt, etc.)
- Return Claude's output to the caller

### R2: Logging — `accepted`
- Log every invocation to a file under `merlin-bot/logs/claude/`
- Log file naming: `<timestamp>-<caller>-<session-id>.log`
- Each log contains:
  - Caller (who triggered it: discord, cron job name, manual)
  - Timestamp (start and end)
  - Prompt sent to Claude
  - Session ID used
  - Full stdout from Claude
  - Full stderr from Claude
  - Exit code
  - Duration

### R3: Callable from Python and CLI — `accepted`
- Usable as an imported Python function (for `merlin.py`)
- Usable as a standalone CLI script (for cron jobs): `uv run claude-wrapper.py --caller cron-weather --session <id> "prompt"`
- Both paths produce identical logging

### R4: PEP 723 inline dependencies — `accepted`
- Script declares its own dependencies via `# /// script` metadata
- Runs with `uv run claude-wrapper.py`

### R5: Error handling — `accepted`
- If Claude Code fails (non-zero exit), log the error and return it to the caller
- Don't crash — the caller decides what to do with errors
