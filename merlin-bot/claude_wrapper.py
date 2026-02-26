# /// script
# dependencies = []
# ///
"""
Claude Code wrapper — single entry point for all Claude Code invocations.

Used by merlin_bot.py (Discord bot) and cron jobs. Never call `claude` directly.
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from structured_log import log_event

import paths

_SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_DIR = paths.logs_dir() / "claude"
SESSION_DIR = paths.logs_dir() / "sessions"
MEMORY_DIR = paths.memory_dir()

# Default model for all invocations
DEFAULT_MODEL = "claude-opus-4-6"

logger = logging.getLogger("claude-wrapper")


def _load_user_memory() -> str | None:
    """Load user memory from memory/user.md if it exists."""
    memory_path = MEMORY_DIR / "user.md"
    if not memory_path.exists():
        return None
    try:
        content = memory_path.read_text().strip()
        if content:
            return f"# User Memory\n\n{content}"
        return None
    except OSError as e:
        logger.warning("Could not read user memory: %s", e)
        return None


def _load_personality() -> str | None:
    """Load bot personality from ~/.merlin/merlin-bot/personality.md if it exists."""
    personality_path = paths.merlin_home() / "merlin-bot" / "personality.md"
    if not personality_path.exists():
        return None
    try:
        content = personality_path.read_text().strip()
        return content if content else None
    except OSError as e:
        logger.warning("Could not read personality file: %s", e)
        return None


def _parse_stream_json(stdout: str) -> dict:
    """Parse NDJSON stream-json output from Claude CLI.

    Returns dict with: result, session_id, usage, model, num_turns, cost_usd, errors.
    """
    result_event = None
    init_event = None

    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result_event = event
        elif event.get("type") == "system" and event.get("subtype") == "init":
            init_event = event

    if not result_event:
        return {
            "result": "",
            "session_id": None,
            "usage": {},
            "model": init_event.get("model") if init_event else None,
            "num_turns": 0,
            "cost_usd": None,
            "errors": [],
        }

    # Extract model from modelUsage keys or init event
    model = None
    model_usage = result_event.get("modelUsage", {})
    if model_usage:
        model = next(iter(model_usage))
    elif init_event:
        model = init_event.get("model")

    return {
        "result": result_event.get("result", ""),
        "session_id": result_event.get("session_id"),
        "usage": result_event.get("usage", {}),
        "model": model,
        "num_turns": result_event.get("num_turns", 0),
        "cost_usd": result_event.get("total_cost_usd"),
        "errors": result_event.get("errors", []),
    }


def _save_session_file(
    stdout: str, caller: str, session_id: str | None, start_time: datetime
) -> str | None:
    """Save the NDJSON stream to a session file. Returns filename or None."""
    if not stdout.strip():
        return None

    timestamp = start_time.strftime("%Y-%m-%d_%H-%M-%S")
    session_tag = session_id or "no-session"
    filename = f"{timestamp}-{caller}-{session_tag}.jsonl"

    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create session directory %s: %s", SESSION_DIR, e)
        return None

    try:
        (SESSION_DIR / filename).write_text(stdout)
    except OSError as e:
        logger.warning("Cannot write session file %s: %s", filename, e)
        return None

    return filename


@dataclass
class ClaudeResult:
    """Structured result from a Claude Code invocation."""

    result: str
    session_id: str | None
    stderr: str
    exit_code: int
    duration: float
    usage: dict = field(default_factory=dict)
    model: str | None = None
    raw_output: str = ""
    cost_usd: float | None = None


def _write_invocation_log(
    caller: str,
    prompt: str,
    result: ClaudeResult,
    start_time: datetime,
) -> Path | None:
    """Write a per-invocation log file. Returns the log path, or None on failure."""
    timestamp = start_time.strftime("%Y-%m-%d_%H-%M-%S")
    session_tag = result.session_id or "no-session"
    filename = f"{timestamp}-{caller}-{session_tag}.log"

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create log directory %s: %s", LOG_DIR, e)
        return None

    log_path = LOG_DIR / filename
    end_time = start_time.timestamp() + result.duration

    lines = [
        f"caller: {caller}",
        f"start: {start_time.isoformat()}",
        f"end: {datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat()}",
        f"duration: {result.duration:.3f}s",
        f"session_id: {result.session_id}",
        f"exit_code: {result.exit_code}",
        f"model: {result.model}",
        f"usage: {json.dumps(result.usage)}",
        "",
        "=== PROMPT ===",
        prompt,
        "",
        "=== STDOUT ===",
        result.raw_output,
        "",
        "=== STDERR ===",
        result.stderr,
    ]

    try:
        log_path.write_text("\n".join(lines))
    except OSError as e:
        logger.warning("Cannot write log file %s: %s", log_path, e)
        return None

    return log_path


def invoke_claude(
    prompt: str,
    *,
    caller: str = "unknown",
    session_id: str | None = None,
    resume: bool = True,
    model: str | None = DEFAULT_MODEL,
    allowed_tools: list[str] | None = None,
    append_system_prompt: str | None = None,
    skip_permissions: bool = True,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
    timeout: float | None = None,
) -> ClaudeResult:
    """Invoke Claude Code as a subprocess and return structured result.

    Args:
        prompt: The prompt to send to Claude.
        caller: Who triggered this invocation (e.g. "discord", "cron-weather").
        session_id: Session ID (UUID). When *resume* is True (default), uses
            ``--resume`` to continue an existing session. When False, uses
            ``--session-id`` to create a new session with this ID.
        resume: If True, resume an existing session. If False, create a new
            session with the given *session_id*.
        model: Model to use, or None for Claude Code default.
        allowed_tools: List of tools to auto-approve, or None for default.
        append_system_prompt: Additional system prompt text (appended, not replaced).
        skip_permissions: If True, pass --dangerously-skip-permissions (default: True).
        max_turns: Max agentic iterations, or None for unlimited.
        max_budget_usd: Cost limit in USD, or None for unlimited.
        timeout: Subprocess timeout in seconds, or None for no limit.

    Returns:
        ClaudeResult with output, metadata, and timing information.
    """
    start_wall = datetime.now(tz=timezone.utc)

    # Auto-inject personality and user memory
    for extra in (_load_personality(), _load_user_memory()):
        if extra:
            if append_system_prompt:
                append_system_prompt = f"{extra}\n\n{append_system_prompt}"
            else:
                append_system_prompt = extra

    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]

    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")

    if session_id:
        if resume:
            cmd.extend(["--resume", session_id])
        else:
            cmd.extend(["--session-id", session_id])

    if model:
        cmd.extend(["--model", model])

    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    if append_system_prompt:
        cmd.extend(["--append-system-prompt", append_system_prompt])

    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])

    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])

    cmd.append(prompt)

    # Pass session ID via environment so child processes (e.g. discord_send.py)
    # can register which session produced which message.
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # Allow nested invocation from within a Claude session
    if session_id:
        env["MERLIN_SESSION_ID"] = session_id

    start = time.monotonic()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_SCRIPT_DIR,
            env=env,
        )
    except FileNotFoundError:
        duration = time.monotonic() - start
        cr = ClaudeResult(
            result="",
            session_id=None,
            stderr="claude: command not found",
            exit_code=127,
            duration=duration,
        )
        _write_invocation_log(caller, prompt, cr, start_wall)
        log_event("invocation", caller=caller, duration=round(duration, 3),
                  exit_code=127, num_turns=0, tokens_in=0, tokens_out=0,
                  session_id=None, model=None)
        return cr
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        cr = ClaudeResult(
            result="",
            session_id=None,
            stderr=f"claude: timed out after {timeout}s",
            exit_code=124,
            duration=duration,
        )
        _write_invocation_log(caller, prompt, cr, start_wall)
        log_event("invocation", caller=caller, duration=round(duration, 3),
                  exit_code=124, num_turns=0, tokens_in=0, tokens_out=0,
                  session_id=None, model=None)
        return cr

    duration = time.monotonic() - start

    # Parse stream-json NDJSON output
    parsed = _parse_stream_json(proc.stdout)
    parsed_session_id = parsed["session_id"] or session_id

    # Save session NDJSON file
    session_file = _save_session_file(proc.stdout, caller, parsed_session_id, start_wall)

    # Combine stderr with errors from result event (stream-json puts errors
    # in the result event's "errors" array, not in stderr)
    stderr = proc.stderr
    if parsed["errors"]:
        error_text = "\n".join(parsed["errors"])
        stderr = f"{stderr}\n{error_text}" if stderr else error_text

    cr = ClaudeResult(
        result=parsed["result"],
        session_id=parsed_session_id,
        stderr=stderr,
        exit_code=proc.returncode,
        duration=duration,
        usage=parsed["usage"],
        model=parsed["model"],
        raw_output=proc.stdout,
        cost_usd=parsed["cost_usd"],
    )
    _write_invocation_log(caller, prompt, cr, start_wall)
    # Don't log "No conversation found" failures — these are expected
    # resume-first probes that will be retried with --session-id.
    if not (proc.returncode != 0 and "No conversation found" in stderr):
        log_event(
            "invocation",
            caller=caller,
            prompt=prompt,
            duration=round(duration, 3),
            exit_code=proc.returncode,
            num_turns=parsed["num_turns"],
            tokens_in=parsed["usage"].get("input_tokens", 0)
            + parsed["usage"].get("cache_read_input_tokens", 0)
            + parsed["usage"].get("cache_creation_input_tokens", 0),
            tokens_out=parsed["usage"].get("output_tokens", 0),
            session_id=parsed_session_id,
            model=parsed["model"],
            session_file=session_file,
            cost_usd=parsed["cost_usd"],
        )
    return cr


def main() -> None:
    """CLI entry point: uv run claude_wrapper.py [options] PROMPT"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Invoke Claude Code and log the result.",
        epilog="""
Examples:
  # Simple invocation
  uv run claude_wrapper.py "What is 2+2?"

  # Resume a session
  uv run claude_wrapper.py --session abc-123 "Continue from where we left off"

  # With caller tag (for log identification)
  uv run claude_wrapper.py --caller cron-weather "Check the weather in Paris"

  # With safety limits
  uv run claude_wrapper.py --max-turns 10 --timeout 60 "Do a quick task"

Output:
  JSON object with: result, session_id, exit_code, duration, usage, model, stderr

Logging:
  All invocations are logged to: logs/claude/<timestamp>-<caller>-<session>.log
  Session transcripts saved to: logs/sessions/<timestamp>-<caller>-<session>.jsonl
  Each log contains: prompt, full stdout/stderr, exit code, duration, usage stats

Notes:
  - This is the single entry point for all Claude Code calls (Discord bot, cron jobs)
  - Never call `claude` directly; always use this wrapper for consistent logging
  - Uses --dangerously-skip-permissions by default (use --no-skip-permissions to disable)
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("prompt", help="The prompt to send to Claude")
    parser.add_argument("--caller", default="cli", help="Caller identifier for logs (default: cli)")
    parser.add_argument("--session", dest="session_id", metavar="ID", help="Session ID to resume")
    parser.add_argument("--model", help="Model to use (default: Claude Code default)")
    parser.add_argument("--allowed-tools", metavar="TOOLS", help="Comma-separated list of tools to allow")
    parser.add_argument("--append-system-prompt", metavar="TEXT", help="Additional system prompt (appended)")
    parser.add_argument("--no-skip-permissions", action="store_true", help="Require permission prompts")
    parser.add_argument("--max-turns", type=int, metavar="N", help="Max agentic iterations")
    parser.add_argument("--max-budget-usd", type=float, metavar="USD", help="Cost limit in USD")
    parser.add_argument("--timeout", type=float, metavar="SECS", help="Subprocess timeout in seconds")

    args = parser.parse_args()

    result = invoke_claude(
        args.prompt,
        caller=args.caller,
        session_id=args.session_id,
        model=args.model,
        allowed_tools=args.allowed_tools.split(",") if args.allowed_tools else None,
        append_system_prompt=args.append_system_prompt,
        skip_permissions=not args.no_skip_permissions,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
        timeout=args.timeout,
    )

    # Print JSON result to stdout so callers can capture it
    print(json.dumps({
        "result": result.result,
        "session_id": result.session_id,
        "exit_code": result.exit_code,
        "duration": result.duration,
        "usage": result.usage,
        "model": result.model,
        "stderr": result.stderr,
    }))

    raise SystemExit(result.exit_code)


if __name__ == "__main__":
    main()
