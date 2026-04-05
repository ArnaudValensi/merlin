"""Tests for lib/claude.py — shared Claude wrapper with layered personality.

Tests for the extra_system_prompts feature and structured logging wiring.
"""

import json
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import lib.claude as cw
from lib.claude import ClaudeResult, invoke_claude

# ---------------------------------------------------------------------------
# Sample stream-json output (NDJSON)
# ---------------------------------------------------------------------------

_INIT_EVENT = json.dumps({
    "type": "system", "subtype": "init",
    "session_id": "sess-abc", "model": "claude-sonnet-4-5-20250929",
    "cwd": "/tmp", "tools": ["Bash", "Read"],
})

_RESULT_EVENT = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "duration_ms": 2000, "num_turns": 1,
    "result": "Hello world", "session_id": "sess-abc",
    "total_cost_usd": 0.05,
    "usage": {"input_tokens": 100, "cache_read_input_tokens": 50,
              "cache_creation_input_tokens": 200, "output_tokens": 50},
    "modelUsage": {"claude-sonnet-4-5-20250929": {
        "inputTokens": 100, "outputTokens": 50, "costUSD": 0.05,
    }},
})

SAMPLE_STREAM_OUTPUT = "\n".join([_INIT_EVENT, _RESULT_EVENT]) + "\n"


@pytest.fixture(autouse=True)
def _clean_logs(tmp_path, monkeypatch):
    """Redirect LOG_DIR and RAW_SESSION_DIR to a temp directory for every test."""
    log_dir = tmp_path / "logs" / "claude"
    session_dir = tmp_path / "logs" / "raw-sessions"
    monkeypatch.setattr(cw, "LOG_DIR", log_dir)
    monkeypatch.setattr(cw, "RAW_SESSION_DIR", session_dir)
    yield
    shutil.rmtree(log_dir, ignore_errors=True)
    shutil.rmtree(session_dir, ignore_errors=True)


@pytest.fixture
def _no_personality(monkeypatch):
    """Disable personality and user context loading."""
    monkeypatch.setattr(cw, "_load_personality", lambda: None)
    monkeypatch.setattr(cw, "_load_user_context", lambda: None)


def _mock_proc(stdout="", stderr="", returncode=0):
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# Extra system prompts
# ---------------------------------------------------------------------------

class TestExtraSystemPrompts:
    """extra_system_prompts parameter loads files and appends to system prompt."""

    def test_extra_file_appended(self, tmp_path, _no_personality):
        """Content from extra_system_prompts file is appended to --append-system-prompt."""
        directives = tmp_path / "discord_directives.md"
        directives.write_text("Be friendly on Discord")

        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", extra_system_prompts=[directives])
            cmd = m.call_args[0][0]

        idx = cmd.index("--append-system-prompt")
        assert "Be friendly on Discord" in cmd[idx + 1]

    def test_multiple_extra_files(self, tmp_path, _no_personality):
        """Multiple extra files are all concatenated."""
        f1 = tmp_path / "a.md"
        f1.write_text("Rule A")
        f2 = tmp_path / "b.md"
        f2.write_text("Rule B")

        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", extra_system_prompts=[f1, f2])
            cmd = m.call_args[0][0]

        prompt_text = cmd[cmd.index("--append-system-prompt") + 1]
        assert "Rule A" in prompt_text
        assert "Rule B" in prompt_text

    def test_missing_extra_file_skipped(self, tmp_path, _no_personality):
        """A missing extra file is silently skipped."""
        missing = tmp_path / "nonexistent.md"

        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", extra_system_prompts=[missing])
            cmd = m.call_args[0][0]

        assert "--append-system-prompt" not in cmd

    def test_empty_extra_file_skipped(self, tmp_path, _no_personality):
        """An empty extra file is skipped."""
        empty = tmp_path / "empty.md"
        empty.write_text("")

        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", extra_system_prompts=[empty])
            cmd = m.call_args[0][0]

        assert "--append-system-prompt" not in cmd

    def test_extra_combined_with_explicit_system_prompt(self, tmp_path, _no_personality):
        """extra_system_prompts is combined with append_system_prompt."""
        directives = tmp_path / "extra.md"
        directives.write_text("Extra rule")

        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", append_system_prompt="Be concise", extra_system_prompts=[directives])
            cmd = m.call_args[0][0]

        prompt_text = cmd[cmd.index("--append-system-prompt") + 1]
        assert "Be concise" in prompt_text
        assert "Extra rule" in prompt_text

    def test_extra_with_string_path(self, tmp_path, _no_personality):
        """String paths are converted to Path objects."""
        directives = tmp_path / "directives.md"
        directives.write_text("Rule from string path")

        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", extra_system_prompts=[str(directives)])
            cmd = m.call_args[0][0]

        assert "Rule from string path" in cmd[cmd.index("--append-system-prompt") + 1]

    def test_none_extra_system_prompts(self, _no_personality):
        """None (default) means no extra prompts."""
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", extra_system_prompts=None)
            cmd = m.call_args[0][0]

        assert "--append-system-prompt" not in cmd


# ---------------------------------------------------------------------------
# Personality loading with fallback
# ---------------------------------------------------------------------------

class TestPersonalityLoading:
    """Personality and user context load from new paths with legacy fallback."""

    def test_personality_from_new_path(self, tmp_path, monkeypatch):
        """Loads personality from ~/.merlin/personality.md."""
        monkeypatch.setattr(cw.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(cw.paths, "notes_dir", lambda: tmp_path / "notes")
        (tmp_path / "personality.md").write_text("Be awesome")

        result = cw._load_personality()
        assert result == "Be awesome"

    def test_personality_fallback_to_legacy(self, tmp_path, monkeypatch):
        """Falls back to ~/.merlin/merlin-bot/personality.md if new path missing."""
        monkeypatch.setattr(cw.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(cw.paths, "notes_dir", lambda: tmp_path / "notes")
        (tmp_path / "merlin-bot").mkdir()
        (tmp_path / "merlin-bot" / "personality.md").write_text("Legacy personality")

        result = cw._load_personality()
        assert result == "Legacy personality"

    def test_personality_new_path_takes_priority(self, tmp_path, monkeypatch):
        """New path takes priority over legacy."""
        monkeypatch.setattr(cw.paths, "merlin_home", lambda: tmp_path)
        (tmp_path / "personality.md").write_text("New personality")
        (tmp_path / "merlin-bot").mkdir()
        (tmp_path / "merlin-bot" / "personality.md").write_text("Legacy personality")

        result = cw._load_personality()
        assert result == "New personality"

    def test_personality_missing_both_returns_none(self, tmp_path, monkeypatch):
        """Returns None if both paths are missing."""
        monkeypatch.setattr(cw.paths, "merlin_home", lambda: tmp_path)

        result = cw._load_personality()
        assert result is None

    def test_user_context_from_new_path(self, tmp_path, monkeypatch):
        """Loads user context from ~/.merlin/user.md."""
        monkeypatch.setattr(cw.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(cw.paths, "notes_dir", lambda: tmp_path / "notes")
        (tmp_path / "user.md").write_text("User is a developer")

        result = cw._load_user_context()
        assert "User is a developer" in result
        assert "# User Memory" in result

    def test_user_context_fallback_to_legacy(self, tmp_path, monkeypatch):
        """Falls back to ~/.merlin/notes/user.md if new path missing."""
        monkeypatch.setattr(cw.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(cw.paths, "notes_dir", lambda: tmp_path / "notes")
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "user.md").write_text("Legacy user info")

        result = cw._load_user_context()
        assert "Legacy user info" in result

    def test_user_context_missing_returns_none(self, tmp_path, monkeypatch):
        """Returns None if no user context file exists."""
        monkeypatch.setattr(cw.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(cw.paths, "notes_dir", lambda: tmp_path / "notes")

        result = cw._load_user_context()
        assert result is None


# ---------------------------------------------------------------------------
# Command construction (from original tests)
# ---------------------------------------------------------------------------

class TestCommandConstruction:
    """invoke_claude() builds the correct claude CLI command."""

    def test_minimal_command(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello")
            cmd = m.call_args[0][0]

        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in cmd
        assert cmd[-1] == "hello"

    def test_dangerously_skip_permissions_always_included(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello")
            cmd = m.call_args[0][0]

        assert "--dangerously-skip-permissions" in cmd

    def test_skip_permissions_false(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", skip_permissions=False)
            cmd = m.call_args[0][0]

        assert "--dangerously-skip-permissions" not in cmd

    def test_session_id_passed(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", session_id="sess-xyz")
            cmd = m.call_args[0][0]

        assert cmd[cmd.index("--resume") + 1] == "sess-xyz"

    def test_session_id_no_resume(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", session_id="sess-xyz", resume=False)
            cmd = m.call_args[0][0]

        assert "--resume" not in cmd
        assert cmd[cmd.index("--session-id") + 1] == "sess-xyz"

    def test_model_flag(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", model="haiku")
            cmd = m.call_args[0][0]

        assert cmd[cmd.index("--model") + 1] == "haiku"

    def test_allowed_tools(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", allowed_tools=["Bash", "Read", "Edit"])
            cmd = m.call_args[0][0]

        assert cmd[cmd.index("--allowedTools") + 1] == "Bash,Read,Edit"

    def test_max_turns(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", max_turns=5)
            cmd = m.call_args[0][0]

        assert cmd[cmd.index("--max-turns") + 1] == "5"

    def test_prompt_is_always_last(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("my prompt", model="sonnet", session_id="s1")
            cmd = m.call_args[0][0]

        assert cmd[-1] == "my prompt"


# ---------------------------------------------------------------------------
# Stream-JSON parsing
# ---------------------------------------------------------------------------

class TestStreamJsonParsing:
    """Structured result is parsed from Claude's stream-json NDJSON output."""

    def test_parses_session_id(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            r = invoke_claude("hello")
        assert r.session_id == "sess-abc"

    def test_parses_result(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            r = invoke_claude("hello")
        assert r.result == "Hello world"

    def test_parses_cost(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            r = invoke_claude("hello")
        assert r.cost_usd == 0.05

    def test_empty_stdout(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="")):
            r = invoke_claude("hello")
        assert r.result == ""
        assert r.usage == {}


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    """Errors are handled gracefully without crashing."""

    def test_missing_claude_binary(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            r = invoke_claude("hello")
        assert r.exit_code == 127
        assert "command not found" in r.stderr

    def test_timeout_expired(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 10)):
            r = invoke_claude("hello", timeout=10)
        assert r.exit_code == 124
        assert "timed out" in r.stderr

    def test_nonzero_exit_code(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}", stderr="error", returncode=1)):
            r = invoke_claude("hello")
        assert r.exit_code == 1
        assert r.stderr == "error"


# ---------------------------------------------------------------------------
# ClaudeResult dataclass
# ---------------------------------------------------------------------------

class TestClaudeResult:
    """ClaudeResult has all expected fields."""

    def test_fields(self):
        r = ClaudeResult(
            result="ok", session_id="s1", stderr="", exit_code=0,
            duration=1.0, usage={"input_tokens": 10}, model="opus",
            raw_output="raw", cost_usd=0.01,
        )
        assert r.result == "ok"
        assert r.session_id == "s1"
        assert r.exit_code == 0
        assert r.duration == 1.0
        assert r.usage == {"input_tokens": 10}
        assert r.model == "opus"
        assert r.raw_output == "raw"
        assert r.cost_usd == 0.01

    def test_defaults(self):
        r = ClaudeResult(result="", session_id=None, stderr="", exit_code=0, duration=0)
        assert r.usage == {}
        assert r.model is None
        assert r.raw_output == ""
        assert r.cost_usd is None
