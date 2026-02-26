# T2-T5: Logging, CLI, Python API, Error Handling

## What was worked on
- T2: Implement logging
- T3: Add CLI interface
- T4: Add Python API (importable function)
- T5: Error handling

## What was done

### T2: Logging
- Added `_write_invocation_log()` function that writes one log file per invocation
- Log directory: `merlin-bot/logs/claude/`
- Log filename: `<YYYY-MM-DD_HH-MM-SS>-<caller>-<session-id>.log`
- Each log contains: caller, start/end timestamps (ISO format), duration, session_id, exit_code, model, usage (JSON), prompt, full stdout, full stderr
- Log directory is created automatically (`mkdir -p` equivalent)
- All three return paths (normal, FileNotFoundError, TimeoutExpired) write logs

### T3: CLI interface
- Added `main()` with argparse: `uv run claude_wrapper.py [options] PROMPT`
- Flags: `--caller`, `--session`, `--model`, `--allowed-tools`, `--append-system-prompt`, `--no-skip-permissions`, `--max-turns`, `--max-budget-usd`, `--timeout`
- Prints JSON result to stdout (result, session_id, exit_code, duration, usage, model, stderr)
- Exits with Claude's exit code

### T4: Python API
- Renamed file from `claude-wrapper.py` to `claude_wrapper.py` (underscore) so it's directly importable
- `from claude_wrapper import invoke_claude, ClaudeResult` works from any script in `merlin-bot/`
- Function signature matches the spec exactly

### T5: Error handling
- All five error cases from the spec were already implemented during T1/T2:
  1. Non-zero exit code: logged and returned to caller
  2. Binary not found: FileNotFoundError → exit_code=127, clear message
  3. Timeout: TimeoutExpired → exit_code=124, configurable via `timeout` param
  4. JSON parse failure: falls back to raw text, now emits `logger.warning`
  5. Log directory not writable: OSError caught, warns via logger, doesn't crash
- Added `logger.warning` for JSON parse failure (was previously silent)

## Validation

All tasks validated via mock-based tests:

**T2 (logging):**
- Log file created with correct naming convention
- Log content contains all required fields (caller, timestamps, prompt, stdout, stderr, exit_code, duration)
- Error paths (FileNotFoundError) also produce log files

**T3 (CLI):**
- `--help` output shows all flags
- Argparse correctly parses all flags and passes to `invoke_claude()`
- JSON output printed to stdout is valid and contains all fields
- Exit code matches Claude's exit code

**T4 (Python API):**
- `from claude_wrapper import invoke_claude, ClaudeResult` works
- All dataclass fields accessible

**T5 (error handling):**
- Non-zero exit: exit_code and stderr preserved
- Binary not found: exit_code=127
- Timeout: exit_code=124
- Invalid JSON: falls back to raw text, warning logged
- Unwritable log dir: no crash, warning logged

## Decisions
- Renamed `claude-wrapper.py` → `claude_wrapper.py` for Python importability. Updated tasks.md references accordingly.
- The `import argparse` is inside `main()` to avoid loading it when only using the Python API.

## Files modified
- `merlin-bot/claude_wrapper.py` (renamed from `claude-wrapper.py`, added logging, CLI, error handling improvements)

## Current state
- T1-T5 all done
- T6 (pytest unit tests) and T7 (end-to-end validation) remain

## Next steps
- T6: Write formal pytest tests
- T7: Live end-to-end test against real Claude Code
