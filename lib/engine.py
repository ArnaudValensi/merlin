"""
Agent Engine — provider-agnostic execution layer.

Replaces the hardcoded Claude Code dependency in lib/claude.py with an
abstraction that can target any AI coding tool (Claude Code, OpenCode,
Gemini CLI, Codex, etc.).

Usage:
    from lib.engine import invoke, get_engine, AgentResult

    # Simple invocation (uses configured engine)
    result = invoke("Check the weather", caller="cron-weather")

    # With session continuity
    result = invoke("Follow up", caller="discord", session_id="abc-123")

    # Direct engine access
    engine = get_engine()
    result = engine.invoke("hello", system_prompt="Be concise")
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

import paths
from structured_log import log_event

logger = logging.getLogger("merlin.engine")

RAW_SESSION_DIR = paths.logs_dir() / "raw-sessions"


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------


@dataclass
class AgentResult:
    """Structured result from an engine invocation."""

    content: str
    exit_code: int
    duration: float
    stderr: str = ""
    usage: dict = field(default_factory=dict)
    model: str | None = None
    cost_usd: float | None = None
    raw_output: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    # Session ID from the engine (Claude Code returns this in stream-json)
    session_id: str | None = None

    @property
    def result(self) -> str:
        """Backward compatibility alias for content."""
        return self.content


# ---------------------------------------------------------------------------
# AgentEngine ABC
# ---------------------------------------------------------------------------


class AgentEngine(ABC):
    """Base class for AI execution engines."""

    name: str
    context_window: int

    @abstractmethod
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
        """Execute a prompt with optional conversation history."""
        ...

    def validate(self) -> str | None:
        """Check if the engine is available. Returns error message or None."""
        return None

    @property
    def supports_tool_use(self) -> bool:
        """Whether this engine can use tools (file edit, bash, etc.)."""
        return True

    @property
    def supports_system_prompt(self) -> bool:
        """Whether this engine supports native system prompt injection."""
        return True

    @property
    def supports_streaming(self) -> bool:
        """Whether stdout can be streamed in real-time."""
        return False


# ---------------------------------------------------------------------------
# Engine registry
# ---------------------------------------------------------------------------

_registry: dict[str, type[AgentEngine]] = {}


def register_engine(name: str, engine_cls: type[AgentEngine]) -> None:
    """Register an engine class by name."""
    _registry[name] = engine_cls


def get_engine(name: str | None = None) -> AgentEngine:
    """Get an engine instance by name.

    If name is None, reads AGENT_ENGINE from environment (default: "claude-code").
    """
    if name is None:
        name = os.environ.get("AGENT_ENGINE", "claude-code")

    if name not in _registry:
        available = ", ".join(sorted(_registry.keys())) or "(none)"
        raise ValueError(
            f"Unknown engine {name!r}. Available engines: {available}"
        )

    return _registry[name]()


# ---------------------------------------------------------------------------
# Personality / user context loading (shared across all engines)
# ---------------------------------------------------------------------------


def _load_file_content(path: Path) -> str | None:
    """Load text content from a file, or None if missing/empty."""
    if not path.exists():
        return None
    try:
        content = path.read_text().strip()
        return content if content else None
    except OSError as e:
        logger.warning("Could not read %s: %s", path, e)
        return None


def _load_user_context() -> str | None:
    """Load user context from ~/.merlin/user.md (or notes/user.md fallback)."""
    content = _load_file_content(paths.merlin_home() / "user.md")
    if content:
        return f"# User Memory\n\n{content}"

    content = _load_file_content(paths.notes_dir() / "user.md")
    if content:
        return f"# User Memory\n\n{content}"

    return None


def _load_personality() -> str | None:
    """Load bot personality from ~/.merlin/personality.md (or legacy path)."""
    content = _load_file_content(paths.merlin_home() / "personality.md")
    if content:
        return content

    return _load_file_content(paths.merlin_home() / "merlin-bot" / "personality.md")


def _build_system_prompt(
    append_system_prompt: str | None = None,
    extra_system_prompts: list[str | Path] | None = None,
) -> str | None:
    """Build the full system prompt from personality, user context, and extras."""
    parts: list[str] = []

    personality = _load_personality()
    if personality:
        parts.append(personality)

    user_context = _load_user_context()
    if user_context:
        parts.append(user_context)

    if append_system_prompt:
        parts.append(append_system_prompt)

    if extra_system_prompts:
        for path_or_str in extra_system_prompts:
            path = Path(path_or_str) if isinstance(path_or_str, str) else path_or_str
            content = _load_file_content(path)
            if content:
                parts.append(content)

    return "\n\n".join(parts) if parts else None


def _save_session_file(
    stdout: str, caller: str, session_id: str | None, start_time: datetime
) -> str | None:
    """Save raw engine output to a session file. Returns filename or None."""
    if not stdout.strip():
        return None

    timestamp = start_time.strftime("%Y-%m-%d_%H-%M-%S")
    session_tag = session_id or "no-session"
    filename = f"{timestamp}-{caller}-{session_tag}.jsonl"

    try:
        RAW_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create session directory %s: %s", RAW_SESSION_DIR, e)
        return None

    try:
        (RAW_SESSION_DIR / filename).write_text(stdout)
    except OSError as e:
        logger.warning("Cannot write session file %s: %s", filename, e)
        return None

    return filename


# ---------------------------------------------------------------------------
# Top-level invoke() — main entry point
# ---------------------------------------------------------------------------


def invoke(
    prompt: str,
    *,
    caller: str = "unknown",
    session_id: str | None = None,
    model: str | None = None,
    allowed_tools: list[str] | None = None,
    append_system_prompt: str | None = None,
    extra_system_prompts: list[str | Path] | None = None,
    skip_permissions: bool = True,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
    timeout: float | None = None,
    # Deprecated — ignored, kept for backward compat of callers not yet updated
    resume: bool = True,
    request_id: str | None = None,
) -> AgentResult:
    """Invoke the configured engine and return structured result.

    This is the main entry point for all engine invocations. It:
    1. Gets the configured engine
    2. Builds the system prompt (personality + user context + extras)
    3. Calls the engine
    4. Writes invocation logs and structured events
    5. Returns AgentResult
    """
    from lib.session import append_turn, create_session, load_session, session_exists

    engine = get_engine()
    start_wall = datetime.now(tz=timezone.utc)
    if not request_id:
        request_id = str(uuid.uuid4())

    # Build system prompt
    system_prompt = _build_system_prompt(append_system_prompt, extra_system_prompts)

    # Load session history if available
    history = load_session(session_id) if session_id else None

    # Invoke engine
    result = engine.invoke(
        prompt,
        history=history,
        system_prompt=system_prompt,
        max_turns=max_turns,
        timeout=timeout,
        allowed_tools=allowed_tools,
        session_id=session_id,
        skip_permissions=skip_permissions,
        model=model,
        max_budget_usd=max_budget_usd,
    )

    # Record turns to session JSONL
    effective_session_id = result.session_id or session_id
    if effective_session_id:
        if not session_exists(effective_session_id):
            create_session(
                effective_session_id,
                engine=engine.name,
                model=result.model,
            )
        # Record user turn
        append_turn(effective_session_id, {
            "role": "user",
            "content": prompt,
            "caller": caller,
        })
        # Record assistant turn
        assistant_turn: dict = {
            "role": "assistant",
            "content": result.content,
            "duration": round(result.duration, 3),
        }
        if result.usage.get("input_tokens"):
            assistant_turn["tokens_in"] = (
                result.usage.get("input_tokens", 0)
                + result.usage.get("cache_read_input_tokens", 0)
                + result.usage.get("cache_creation_input_tokens", 0)
            )
        if result.usage.get("output_tokens"):
            assistant_turn["tokens_out"] = result.usage.get("output_tokens", 0)
        if result.cost_usd is not None:
            assistant_turn["cost_usd"] = result.cost_usd
        append_turn(effective_session_id, assistant_turn)

        # Record tool calls if present
        for tc in result.tool_calls:
            append_turn(effective_session_id, {
                "role": "tool_call",
                "name": tc.get("name", "unknown"),
                "input": tc.get("input", {}),
            })
            if "output" in tc:
                append_turn(effective_session_id, {
                    "role": "tool_result",
                    "name": tc.get("name", "unknown"),
                    "output": tc.get("output", ""),
                })

    # Save raw session file (for session viewer — legacy format)
    session_file = _save_session_file(
        result.raw_output, caller, effective_session_id, start_wall
    )

    # Log structured event (same format as before, with engine field)
    # Don't log "No conversation found" failures — resume-first probes
    if not (result.exit_code != 0 and "No conversation found" in result.stderr):
        log_event(
            "invocation",
            caller=caller,
            prompt=prompt,
            duration=round(result.duration, 3),
            exit_code=result.exit_code,
            num_turns=result.usage.get("num_turns", 0),
            tokens_in=result.usage.get("input_tokens", 0)
            + result.usage.get("cache_read_input_tokens", 0)
            + result.usage.get("cache_creation_input_tokens", 0),
            tokens_out=result.usage.get("output_tokens", 0),
            session_id=session_id,
            model=result.model,
            session_file=session_file,
            cost_usd=result.cost_usd,
            engine=engine.name,
            stderr=result.stderr[:500] if result.stderr else "",
            request_id=request_id,
        )

    return result


# ---------------------------------------------------------------------------
# Register built-in engines on import
# ---------------------------------------------------------------------------

def _register_builtin_engines() -> None:
    """Import and register all built-in engines."""
    from lib.engines.claude_code import ClaudeCodeEngine
    from lib.engines.opencode import OpenCodeEngine
    register_engine("claude-code", ClaudeCodeEngine)
    register_engine("opencode", OpenCodeEngine)


_register_builtin_engines()
