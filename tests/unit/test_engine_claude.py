"""Tests for lib/engines/claude_code.py — ClaudeCodeEngine."""

import json
import subprocess
from unittest import mock


from lib.engine import AgentResult
from lib.engines.claude_code import (
    ClaudeCodeEngine,
    _format_history,
    _parse_stream_json,
)

# ---------------------------------------------------------------------------
# Sample stream-json output
# ---------------------------------------------------------------------------

_INIT_EVENT = json.dumps(
    {
        "type": "system",
        "subtype": "init",
        "session_id": "sess-abc",
        "model": "claude-sonnet-4-5-20250929",
        "cwd": "/tmp",
        "tools": ["Bash", "Read"],
    }
)

_RESULT_EVENT = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 2000,
        "num_turns": 1,
        "result": "Hello world",
        "session_id": "sess-abc",
        "total_cost_usd": 0.05,
        "usage": {
            "input_tokens": 100,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 200,
            "output_tokens": 50,
        },
        "modelUsage": {
            "claude-sonnet-4-5-20250929": {
                "inputTokens": 100,
                "outputTokens": 50,
                "costUSD": 0.05,
            }
        },
    }
)

SAMPLE_STREAM = "\n".join([_INIT_EVENT, _RESULT_EVENT]) + "\n"


def _mock_proc(stdout="", stderr="", returncode=0):
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# Engine properties
# ---------------------------------------------------------------------------


class TestClaudeCodeEngineProperties:
    def test_name(self):
        assert ClaudeCodeEngine().name == "claude-code"

    def test_context_window(self):
        assert ClaudeCodeEngine().context_window == 1_000_000

    def test_supports_tool_use(self):
        assert ClaudeCodeEngine().supports_tool_use is True

    def test_supports_system_prompt(self):
        assert ClaudeCodeEngine().supports_system_prompt is True

    def test_supports_streaming(self):
        assert ClaudeCodeEngine().supports_streaming is False


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_validate_binary_found(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/claude")
        assert ClaudeCodeEngine().validate() is None

    def test_validate_binary_missing(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: None)
        error = ClaudeCodeEngine().validate()
        assert error is not None
        assert "not found" in error


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


class TestCommandConstruction:
    """ClaudeCodeEngine.invoke() builds the correct claude CLI command."""

    def test_minimal_command(self):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello")
            cmd = m.call_args[0][0]

        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in cmd
        assert cmd[-1] == "hello"

    def test_skip_permissions_default(self):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello")
            cmd = m.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd

    def test_skip_permissions_false(self):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello", skip_permissions=False)
            cmd = m.call_args[0][0]
        assert "--dangerously-skip-permissions" not in cmd

    def test_no_resume_or_session_flags(self):
        """Engine no longer uses --resume or --session-id (Merlin manages sessions)."""
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello", session_id="sess-xyz")
            cmd = m.call_args[0][0]
        assert "--resume" not in cmd
        assert "--session-id" not in cmd

    def test_model_flag(self):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello", model="haiku")
            cmd = m.call_args[0][0]
        assert cmd[cmd.index("--model") + 1] == "haiku"

    def test_allowed_tools(self):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello", allowed_tools=["Bash", "Read", "Edit"])
            cmd = m.call_args[0][0]
        assert cmd[cmd.index("--allowedTools") + 1] == "Bash,Read,Edit"

    def test_max_turns(self):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello", max_turns=5)
            cmd = m.call_args[0][0]
        assert cmd[cmd.index("--max-turns") + 1] == "5"

    def test_max_budget(self):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello", max_budget_usd=1.5)
            cmd = m.call_args[0][0]
        assert cmd[cmd.index("--max-budget-usd") + 1] == "1.5"

    def test_system_prompt(self):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello", system_prompt="Be concise")
            cmd = m.call_args[0][0]
        assert cmd[cmd.index("--append-system-prompt") + 1] == "Be concise"

    def test_prompt_is_last(self):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("my prompt", model="sonnet", session_id="s1")
            cmd = m.call_args[0][0]
        assert cmd[-1] == "my prompt"


# ---------------------------------------------------------------------------
# Stream-JSON parsing
# ---------------------------------------------------------------------------


class TestStreamJsonParsing:
    """_parse_stream_json extracts metadata from NDJSON output."""

    def test_parses_result(self):
        parsed = _parse_stream_json(SAMPLE_STREAM)
        assert parsed["result"] == "Hello world"

    def test_parses_session_id(self):
        parsed = _parse_stream_json(SAMPLE_STREAM)
        assert parsed["session_id"] == "sess-abc"

    def test_parses_cost(self):
        parsed = _parse_stream_json(SAMPLE_STREAM)
        assert parsed["cost_usd"] == 0.05

    def test_parses_model(self):
        parsed = _parse_stream_json(SAMPLE_STREAM)
        assert parsed["model"] == "claude-sonnet-4-5-20250929"

    def test_parses_num_turns(self):
        parsed = _parse_stream_json(SAMPLE_STREAM)
        assert parsed["num_turns"] == 1

    def test_empty_stdout(self):
        parsed = _parse_stream_json("")
        assert parsed["result"] == ""
        assert parsed["session_id"] is None
        assert parsed["usage"] == {}

    def test_no_result_event(self):
        parsed = _parse_stream_json(_INIT_EVENT + "\n")
        assert parsed["result"] == ""
        assert parsed["model"] == "claude-sonnet-4-5-20250929"


# ---------------------------------------------------------------------------
# invoke() result
# ---------------------------------------------------------------------------


class TestHistoryFormatting:
    """_format_history formats turns as readable conversation text."""

    def test_empty_history(self):
        assert _format_history([]) == ""

    def test_user_and_assistant(self):
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = _format_history(history)
        assert "[User]: Hello" in result
        assert "[Assistant]: Hi there" in result

    def test_skips_system(self):
        history = [
            {"role": "system", "content": "Be nice"},
            {"role": "user", "content": "Hello"},
        ]
        result = _format_history(history)
        assert "Be nice" not in result
        assert "[User]: Hello" in result

    def test_tool_calls(self):
        history = [
            {"role": "tool_call", "name": "Bash", "input": {"command": "ls"}},
            {"role": "tool_result", "name": "Bash", "output": "file.txt"},
        ]
        result = _format_history(history)
        assert "[Tool call: Bash]" in result
        assert "[Tool result: Bash]" in result

    def test_compaction_marker(self):
        history = [{"role": "compaction", "dropped": 10}]
        result = _format_history(history)
        assert "10 earlier turns omitted" in result

    def test_history_in_prompt(self):
        """When history is provided, prompt includes conversation context."""
        engine = ClaudeCodeEngine()
        history = [
            {"role": "user", "content": "First message"},
            {"role": "assistant", "content": "First reply"},
        ]
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("Follow up", history=history)
            cmd = m.call_args[0][0]
        prompt = cmd[-1]
        assert "[Conversation history]" in prompt
        assert "[User]: First message" in prompt
        assert "[New message]" in prompt
        assert "Follow up" in prompt


class TestInvokeResult:
    """Engine invoke returns properly structured AgentResult."""

    def test_success(self):
        engine = ClaudeCodeEngine()
        with mock.patch(
            "subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM)
        ):
            r = engine.invoke("hello", session_id="my-session")
        assert isinstance(r, AgentResult)
        assert r.content == "Hello world"
        assert r.exit_code == 0
        assert r.session_id == "my-session"  # Merlin's session ID, not Claude's
        assert r.cost_usd == 0.05
        assert r.model == "claude-sonnet-4-5-20250929"

    def test_result_property(self):
        """Backward compat: .result returns .content."""
        engine = ClaudeCodeEngine()
        with mock.patch(
            "subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM)
        ):
            r = engine.invoke("hello")
        assert r.result == r.content


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_binary_not_found(self):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            r = engine.invoke("hello")
        assert r.exit_code == 127
        assert "command not found" in r.stderr

    def test_timeout(self):
        engine = ClaudeCodeEngine()
        with mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 10)
        ):
            r = engine.invoke("hello", timeout=10)
        assert r.exit_code == 124
        assert "timed out" in r.stderr

    def test_nonzero_exit(self):
        engine = ClaudeCodeEngine()
        with mock.patch(
            "subprocess.run",
            return_value=_mock_proc(stdout="{}", stderr="error", returncode=1),
        ):
            r = engine.invoke("hello")
        assert r.exit_code == 1
        assert r.stderr == "error"


# ---------------------------------------------------------------------------
# Skills plugin adapter
# ---------------------------------------------------------------------------


class TestSkillsPlugin:
    def _make_canonical_skill(self, tmp_path, name="cron"):
        from lib import skills

        skill_dir = tmp_path / "src" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test.\n---\n"
        )
        canonical = skills.canonical_dir()
        canonical.mkdir(parents=True, exist_ok=True)
        (canonical / name).symlink_to(skill_dir)
        return canonical

    def test_ensure_generates_manifest_and_symlink(self, tmp_path):
        import json as _json

        from lib import skills
        from lib.engines.claude_code import ensure_skills_plugin

        canonical = self._make_canonical_skill(tmp_path)
        plugin_dir = ensure_skills_plugin()

        assert plugin_dir is not None
        manifest = _json.loads(
            (plugin_dir / ".claude-plugin" / "plugin.json").read_text()
        )
        assert manifest["name"] == "merlin"
        skills_link = plugin_dir / "skills"
        assert skills_link.is_symlink()
        assert skills_link.resolve() == canonical.resolve()
        assert (skills_link / "cron" / "SKILL.md").is_file()
        del skills  # imported for parity with other tests

    def test_ensure_none_when_no_skills(self, tmp_path):
        from lib.engines.claude_code import ensure_skills_plugin

        assert ensure_skills_plugin() is None

    def test_ensure_idempotent(self, tmp_path):
        from lib.engines.claude_code import ensure_skills_plugin

        self._make_canonical_skill(tmp_path)
        first = ensure_skills_plugin()
        second = ensure_skills_plugin()
        assert first == second

    def test_invoke_passes_plugin_dir(self, tmp_path):
        from lib.engines.claude_code import ensure_skills_plugin

        self._make_canonical_skill(tmp_path)
        plugin_dir = ensure_skills_plugin()

        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello")
        cmd = m.call_args[0][0]
        assert "--plugin-dir" in cmd
        assert cmd[cmd.index("--plugin-dir") + 1] == str(plugin_dir)

    def test_invoke_no_plugin_dir_without_skills(self, tmp_path):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello")
        assert "--plugin-dir" not in m.call_args[0][0]


class TestCwdHandling:
    """cwd means "where the job operates" — no app-dir fallback."""

    def test_explicit_cwd_passed_to_subprocess(self, tmp_path):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello", cwd=tmp_path)
        assert m.call_args.kwargs["cwd"] == tmp_path

    def test_no_cwd_means_inherit_not_app_dir(self):
        engine = ClaudeCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            engine.invoke("hello")
        assert m.call_args.kwargs["cwd"] is None
