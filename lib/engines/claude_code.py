"""
Claude Code engine — wraps the `claude` CLI as an AgentEngine.

This is the default engine. Session continuity is managed by Merlin's
session manager (JSONL transcripts) — history is formatted as a
conversation block prepended to the prompt.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import paths
from lib.engine import AgentEngine, AgentResult

logger = logging.getLogger("merlin.engine.claude_code")

DEFAULT_MODEL = "claude-opus-4-6"

# Manifest content for the generated skills plugin. The plugin wraps the
# canonical skill dir (~/.merlin/skills) so Claude Code surfaces Merlin's
# skills natively (namespaced merlin:<skill>).
_PLUGIN_MANIFEST = {
    "name": "merlin",
    "description": "Merlin operational skills",
    "version": "1.0.0",
}


def skills_plugin_dir() -> Path:
    """Location of the generated Claude Code skills plugin."""
    return paths.merlin_home() / "skills-plugin"


def ensure_skills_plugin() -> Path | None:
    """Generate ~/.merlin/skills-plugin wrapping the canonical skill dir.

    Layout: <plugin>/.claude-plugin/plugin.json plus <plugin>/skills as a
    symlink to ~/.merlin/skills. Idempotent. Returns the plugin dir, or
    None when there are no skills to expose (or generation fails).
    """
    from lib import skills

    canonical = skills.canonical_dir()
    try:
        has_skills = canonical.is_dir() and any(
            entry.is_dir() for entry in canonical.iterdir()
        )
    except OSError:
        return None
    if not has_skills:
        return None

    plugin_dir = skills_plugin_dir()
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    desired = json.dumps(_PLUGIN_MANIFEST, indent=2) + "\n"

    try:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if not manifest.exists() or manifest.read_text() != desired:
            manifest.write_text(desired)

        skills_link = plugin_dir / "skills"
        if skills_link.is_symlink():
            if skills_link.resolve() != canonical.resolve():
                skills_link.unlink()
                skills_link.symlink_to(canonical)
        elif skills_link.exists():
            logger.warning(
                "%s exists and is not a symlink; not touching it", skills_link
            )
            return None
        else:
            skills_link.symlink_to(canonical)
    except OSError as e:
        logger.warning("Could not generate skills plugin: %s", e)
        return None

    return plugin_dir


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


def _format_history(history: list[dict]) -> str:
    """Format conversation history as a text block for Claude Code.

    Claude Code's -p flag accepts a single prompt string. We format
    the history as a readable conversation that Claude can continue.
    """
    if not history:
        return ""

    parts: list[str] = []
    for turn in history:
        role = turn.get("role", "unknown")
        if role == "system":
            continue  # System prompt is passed separately
        elif role == "user":
            parts.append(f"[User]: {turn.get('content', '')}")
        elif role == "assistant":
            parts.append(f"[Assistant]: {turn.get('content', '')}")
        elif role == "tool_call":
            name = turn.get("name", "unknown")
            input_str = json.dumps(turn.get("input", {}), indent=2)
            parts.append(f"[Tool call: {name}]: {input_str}")
        elif role == "tool_result":
            name = turn.get("name", "")
            output = turn.get("output", "")
            parts.append(f"[Tool result: {name}]: {output}")
        elif role == "compaction":
            parts.append(f"[{turn.get('dropped', 0)} earlier turns omitted]")

    return "\n\n".join(parts)


class ClaudeCodeEngine(AgentEngine):
    """Claude Code CLI engine — the default execution backend."""

    name = "claude-code"
    context_window = 1_000_000  # Opus 4.6

    def invoke(
        self,
        prompt: str,
        *,
        history: list[dict] | None = None,
        system_prompt: str | None = None,
        max_turns: int | None = None,
        timeout: float | None = None,
        allowed_tools: list[str] | None = None,
        cwd: Path | None = None,
        session_id: str | None = None,
        skip_permissions: bool = True,
        model: str | None = DEFAULT_MODEL,
        max_budget_usd: float | None = None,
    ) -> AgentResult:
        """Invoke Claude Code CLI as a subprocess.

        History is formatted as a conversation block prepended to the prompt.
        No --resume or --session-id — Merlin manages sessions.
        """
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]

        if skip_permissions:
            cmd.append("--dangerously-skip-permissions")

        if model:
            cmd.extend(["--model", model])

        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        if max_turns is not None:
            cmd.extend(["--max-turns", str(max_turns)])

        if max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(max_budget_usd)])

        # Expose Merlin's skills natively via the generated plugin
        plugin_dir = ensure_skills_plugin()
        if plugin_dir is not None:
            cmd.extend(["--plugin-dir", str(plugin_dir)])

        # Format history as conversation context prepended to prompt
        full_prompt = prompt
        if history:
            history_text = _format_history(history)
            if history_text:
                full_prompt = (
                    f"[Conversation history]\n{history_text}\n\n[New message]\n{prompt}"
                )

        cmd.append(full_prompt)

        # Pass session ID via environment for child processes
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        if session_id:
            env["MERLIN_SESSION_ID"] = session_id

        start = time.monotonic()

        try:
            # cwd means "where the job operates" — callers pass it
            # explicitly; None inherits the server process cwd. The old
            # fallback to the app dir made every managed agent read the
            # development CLAUDE.md by accident.
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env,
            )
        except FileNotFoundError:
            duration = time.monotonic() - start
            return AgentResult(
                content="",
                exit_code=127,
                duration=duration,
                stderr="claude: command not found",
            )
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - start
            return AgentResult(
                content="",
                exit_code=124,
                duration=duration,
                stderr=f"claude: timed out after {timeout}s",
            )

        duration = time.monotonic() - start

        # Parse stream-json NDJSON output
        parsed = _parse_stream_json(proc.stdout)

        # Combine stderr with errors from result event
        stderr = proc.stderr
        if parsed["errors"]:
            error_text = "\n".join(parsed["errors"])
            stderr = f"{stderr}\n{error_text}" if stderr else error_text

        # Build usage dict with num_turns for structured logging
        usage = parsed["usage"]
        usage["num_turns"] = parsed["num_turns"]

        return AgentResult(
            content=parsed["result"],
            exit_code=proc.returncode,
            duration=duration,
            stderr=stderr,
            usage=usage,
            model=parsed["model"],
            cost_usd=parsed["cost_usd"],
            raw_output=proc.stdout,
            session_id=session_id,
        )

    def validate(self) -> str | None:
        """Check if claude CLI is available."""
        if not shutil.which("claude"):
            return (
                "claude CLI not found on PATH.\n"
                "Install Claude Code: https://docs.anthropic.com/en/docs/claude-code"
            )
        return None

    @property
    def supports_tool_use(self) -> bool:
        return True

    @property
    def supports_system_prompt(self) -> bool:
        return True

    @property
    def supports_streaming(self) -> bool:
        return False

    @property
    def supports_native_skills(self) -> bool:
        return True  # Via the generated --plugin-dir skills plugin
