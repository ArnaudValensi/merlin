"""
OpenCode engine — wraps the `opencode` CLI as an AgentEngine.

OpenCode is an open-source AI coding agent that supports multiple
providers (Anthropic, OpenAI, Google, Ollama, etc.).

Non-interactive invocation:
    opencode run "prompt"
    opencode run --format json "prompt"
    opencode run --model provider/model "prompt"
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from lib.engine import AgentEngine, AgentResult

logger = logging.getLogger("merlin.engine.opencode")


def _format_history(history: list[dict]) -> str:
    """Format conversation history as a text block for OpenCode.

    OpenCode's `run` command accepts a single prompt string.
    We format the history as a readable conversation.
    """
    if not history:
        return ""

    parts: list[str] = []
    for turn in history:
        role = turn.get("role", "unknown")
        if role == "system":
            continue
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


def _parse_json_output(stdout: str) -> dict:
    """Parse OpenCode JSON output (--format json).

    Returns dict with: result, usage, model, cost_usd.
    """
    # Try to parse as JSON events (one per line)
    result_text = ""
    model = None

    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Not JSON — treat as plain text
            result_text += line + "\n"
            continue

        # OpenCode JSON format may vary — extract what we can
        if isinstance(event, dict):
            if "content" in event:
                result_text += event["content"]
            elif "text" in event:
                result_text += event["text"]
            elif "message" in event:
                result_text += str(event["message"])
            if "model" in event:
                model = event["model"]

    return {
        "result": result_text.strip() or stdout.strip(),
        "model": model,
    }


class OpenCodeEngine(AgentEngine):
    """OpenCode CLI engine — open-source multi-provider coding agent."""

    name = "opencode"
    context_window = 200_000  # Conservative default

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
        model: str | None = None,
        max_budget_usd: float | None = None,
    ) -> AgentResult:
        """Invoke OpenCode CLI in non-interactive mode."""
        # OpenCode reads ~/.agents/skills/<name>/SKILL.md natively (no
        # nesting) — refresh the per-skill links before invoking.
        from lib import skills

        try:
            skills.sync_shim_links(skills.agents_skills_dir())
        except OSError as e:
            logger.warning("Could not sync ~/.agents/skills links: %s", e)

        cmd = ["opencode", "run"]

        if model:
            cmd.extend(["--model", model])

        # Build the full prompt with history and system prompt
        full_prompt_parts: list[str] = []

        if system_prompt:
            full_prompt_parts.append(f"[System instructions]\n{system_prompt}")

        if history:
            history_text = _format_history(history)
            if history_text:
                full_prompt_parts.append(f"[Conversation history]\n{history_text}")

        full_prompt_parts.append(prompt if not history else f"[New message]\n{prompt}")
        full_prompt = "\n\n".join(full_prompt_parts)

        cmd.append(full_prompt)

        env = os.environ.copy()
        if session_id:
            env["MERLIN_SESSION_ID"] = session_id

        start = time.monotonic()

        try:
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
                stderr="opencode: command not found",
            )
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - start
            return AgentResult(
                content="",
                exit_code=124,
                duration=duration,
                stderr=f"opencode: timed out after {timeout}s",
            )

        duration = time.monotonic() - start

        # Parse output — try JSON first, fall back to plain text
        content = proc.stdout.strip()
        parsed_model = model

        return AgentResult(
            content=content,
            exit_code=proc.returncode,
            duration=duration,
            stderr=proc.stderr,
            model=parsed_model,
            raw_output=proc.stdout,
            session_id=session_id,
        )

    def validate(self) -> str | None:
        """Check if opencode CLI is available."""
        if not shutil.which("opencode"):
            return (
                "opencode CLI not found on PATH.\n"
                "Install OpenCode: https://opencode.ai/docs/installation"
            )
        return None

    @property
    def supports_tool_use(self) -> bool:
        return True

    @property
    def supports_system_prompt(self) -> bool:
        return False  # Injected as prompt prefix

    @property
    def supports_streaming(self) -> bool:
        return False

    @property
    def supports_native_skills(self) -> bool:
        return True  # Via ~/.agents/skills per-skill links
