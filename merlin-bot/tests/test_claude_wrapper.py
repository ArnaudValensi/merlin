# /// script
# dependencies = ["pytest"]
# ///
"""Tests for claude_wrapper.py — all subprocess calls are mocked."""

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import claude_wrapper as cw
from claude_wrapper import ClaudeResult, invoke_claude

# ---------------------------------------------------------------------------
# Sample stream-json output (NDJSON)
# ---------------------------------------------------------------------------

_INIT_EVENT = json.dumps({
    "type": "system", "subtype": "init",
    "session_id": "sess-abc", "model": "claude-sonnet-4-5-20250929",
    "cwd": "/tmp", "tools": ["Bash", "Read"],
})

_ASSISTANT_EVENT = json.dumps({
    "type": "assistant",
    "message": {
        "model": "claude-sonnet-4-5-20250929", "id": "msg_01",
        "content": [{"type": "text", "text": "Hello world"}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    },
    "session_id": "sess-abc",
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

SAMPLE_STREAM_OUTPUT = "\n".join([_INIT_EVENT, _ASSISTANT_EVENT, _RESULT_EVENT]) + "\n"

# Legacy single-JSON output for backward-compat edge case tests
SAMPLE_JSON_OUTPUT = json.dumps({
    "session_id": "sess-abc",
    "result": "Hello world",
    "usage": {"input_tokens": 100, "output_tokens": 50},
    "model": "claude-sonnet-4-5-20250929",
})


@pytest.fixture(autouse=True)
def _clean_logs(tmp_path, monkeypatch):
    """Redirect LOG_DIR and SESSION_DIR to a temp directory for every test."""
    log_dir = tmp_path / "logs" / "claude"
    session_dir = tmp_path / "logs" / "sessions"
    monkeypatch.setattr(cw, "LOG_DIR", log_dir)
    monkeypatch.setattr(cw, "SESSION_DIR", session_dir)
    yield
    shutil.rmtree(log_dir, ignore_errors=True)
    shutil.rmtree(session_dir, ignore_errors=True)


def _mock_proc(stdout="", stderr="", returncode=0):
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# Command construction
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

    def test_output_format_stream_json(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello")
            cmd = m.call_args[0][0]

        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "stream-json"

    def test_verbose_flag_included(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello")
            cmd = m.call_args[0][0]

        assert "--verbose" in cmd

    def test_session_id_passed(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", session_id="sess-xyz")
            cmd = m.call_args[0][0]

        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "sess-xyz"

    def test_no_session_id_omits_resume(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello")
            cmd = m.call_args[0][0]

        assert "--resume" not in cmd

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

    def test_append_system_prompt(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m, \
             mock.patch.object(cw, "_load_user_memory", return_value=None), \
             mock.patch.object(cw, "_load_personality", return_value=None):
            invoke_claude("hello", append_system_prompt="Be concise")
            cmd = m.call_args[0][0]

        assert cmd[cmd.index("--append-system-prompt") + 1] == "Be concise"

    def test_max_turns(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", max_turns=5)
            cmd = m.call_args[0][0]

        assert cmd[cmd.index("--max-turns") + 1] == "5"

    def test_max_budget_usd(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", max_budget_usd=2.5)
            cmd = m.call_args[0][0]

        assert cmd[cmd.index("--max-budget-usd") + 1] == "2.5"

    def test_all_flags_together(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m, \
             mock.patch.object(cw, "_load_user_memory", return_value=None), \
             mock.patch.object(cw, "_load_personality", return_value=None):
            invoke_claude(
                "do stuff",
                session_id="s1",
                model="opus",
                allowed_tools=["Bash"],
                append_system_prompt="Extra",
                max_turns=3,
                max_budget_usd=1.0,
            )
            cmd = m.call_args[0][0]

        assert "--dangerously-skip-permissions" in cmd
        assert cmd[cmd.index("--resume") + 1] == "s1"
        assert cmd[cmd.index("--model") + 1] == "opus"
        assert cmd[cmd.index("--allowedTools") + 1] == "Bash"
        assert cmd[cmd.index("--append-system-prompt") + 1] == "Extra"
        assert cmd[cmd.index("--max-turns") + 1] == "3"
        assert cmd[cmd.index("--max-budget-usd") + 1] == "1.0"
        assert cmd[-1] == "do stuff"

    def test_prompt_is_always_last(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("my prompt", model="sonnet", session_id="s1")
            cmd = m.call_args[0][0]

        assert cmd[-1] == "my prompt"

    def test_timeout_passed_to_subprocess(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")) as m:
            invoke_claude("hello", timeout=30.0)

        assert m.call_args[1]["timeout"] == 30.0


# ---------------------------------------------------------------------------
# Stream-JSON NDJSON parsing
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

    def test_parses_usage(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            r = invoke_claude("hello")

        assert r.usage["input_tokens"] == 100
        assert r.usage["output_tokens"] == 50

    def test_parses_model_from_model_usage(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            r = invoke_claude("hello")

        assert r.model == "claude-sonnet-4-5-20250929"

    def test_preserves_raw_output(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            r = invoke_claude("hello")

        assert r.raw_output == SAMPLE_STREAM_OUTPUT

    def test_exit_code_zero(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            r = invoke_claude("hello")

        assert r.exit_code == 0

    def test_duration_is_positive(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")):
            r = invoke_claude("hello")

        assert r.duration >= 0

    def test_empty_stdout(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="")):
            r = invoke_claude("hello")

        assert r.result == ""
        assert r.usage == {}

    def test_parses_model_from_init_when_no_model_usage(self):
        """Falls back to system init event for model when modelUsage is absent."""
        init = json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-4-6"})
        result = json.dumps({
            "type": "result", "subtype": "success", "result": "ok",
            "session_id": "s1", "num_turns": 1, "usage": {},
        })
        stdout = init + "\n" + result + "\n"

        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=stdout)):
            r = invoke_claude("hello")

        assert r.model == "claude-opus-4-6"

    def test_no_result_event_returns_empty(self):
        """If stream has no result event, return empty result."""
        init = json.dumps({"type": "system", "subtype": "init", "model": "opus"})
        stdout = init + "\n"

        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=stdout)):
            r = invoke_claude("hello")

        assert r.result == ""
        assert r.model == "opus"


# ---------------------------------------------------------------------------
# _parse_stream_json unit tests
# ---------------------------------------------------------------------------

class TestParseStreamJson:
    """Direct unit tests for _parse_stream_json helper."""

    def test_full_stream(self):
        parsed = cw._parse_stream_json(SAMPLE_STREAM_OUTPUT)
        assert parsed["result"] == "Hello world"
        assert parsed["session_id"] == "sess-abc"
        assert parsed["model"] == "claude-sonnet-4-5-20250929"
        assert parsed["num_turns"] == 1
        assert parsed["cost_usd"] == 0.05
        assert parsed["usage"]["output_tokens"] == 50

    def test_empty_input(self):
        parsed = cw._parse_stream_json("")
        assert parsed["result"] == ""
        assert parsed["session_id"] is None
        assert parsed["model"] is None

    def test_no_result_event(self):
        init = json.dumps({"type": "system", "subtype": "init", "model": "opus"})
        parsed = cw._parse_stream_json(init)
        assert parsed["result"] == ""
        assert parsed["model"] == "opus"
        assert parsed["num_turns"] == 0

    def test_errors_extracted(self):
        result = json.dumps({
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "result": "", "session_id": "s1",
            "num_turns": 0, "usage": {}, "total_cost_usd": 0,
            "errors": ["No conversation found with session ID: abc-123"],
        })
        parsed = cw._parse_stream_json(result)
        assert parsed["errors"] == ["No conversation found with session ID: abc-123"]

    def test_no_errors_returns_empty_list(self):
        parsed = cw._parse_stream_json(SAMPLE_STREAM_OUTPUT)
        assert parsed["errors"] == []

    def test_malformed_lines_skipped(self):
        good_line = json.dumps({
            "type": "result", "result": "ok", "session_id": "s1",
            "num_turns": 1, "usage": {}, "total_cost_usd": 0.01,
            "modelUsage": {"opus": {}},
        })
        stdout = "not json\n" + good_line + "\nalso bad\n"
        parsed = cw._parse_stream_json(stdout)
        assert parsed["result"] == "ok"


# ---------------------------------------------------------------------------
# Session file saving
# ---------------------------------------------------------------------------

class TestSessionFileSaving:
    """Session NDJSON files are saved to logs/sessions/."""

    def test_session_file_created(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("hello", caller="test")

        session_files = list(cw.SESSION_DIR.glob("*.jsonl"))
        assert len(session_files) == 1

    def test_session_filename_contains_caller(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("hello", caller="discord")

        session_files = list(cw.SESSION_DIR.glob("*.jsonl"))
        assert "discord" in session_files[0].name

    def test_session_filename_contains_session_id(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("hello", caller="test", session_id="sess-abc")

        session_files = list(cw.SESSION_DIR.glob("*.jsonl"))
        assert "sess-abc" in session_files[0].name

    def test_session_file_content_is_ndjson(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("hello", caller="test")

        session_files = list(cw.SESSION_DIR.glob("*.jsonl"))
        content = session_files[0].read_text()
        lines = [l for l in content.strip().splitlines() if l.strip()]
        assert len(lines) == 3  # init, assistant, result
        for line in lines:
            json.loads(line)  # should not raise

    def test_empty_stdout_no_session_file(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="")):
            invoke_claude("hello", caller="test")

        if cw.SESSION_DIR.exists():
            assert list(cw.SESSION_DIR.glob("*.jsonl")) == []

    def test_unwritable_session_dir_does_not_crash(self, monkeypatch):
        monkeypatch.setattr(cw, "SESSION_DIR", Path("/proc/nonexistent/sessions"))
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            r = invoke_claude("hello")

        assert r.exit_code == 0  # didn't crash


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    """Errors are handled gracefully without crashing."""

    def test_nonzero_exit_code(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}", stderr="error", returncode=1)):
            r = invoke_claude("hello")

        assert r.exit_code == 1
        assert r.stderr == "error"

    def test_invalid_ndjson_returns_empty_result(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="not json at all")):
            r = invoke_claude("hello")

        assert r.result == ""

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

    def test_result_event_errors_propagated_to_stderr(self):
        """Errors in the result event's errors array are merged into stderr."""
        result_event = json.dumps({
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "result": "", "session_id": "s1",
            "num_turns": 0, "usage": {}, "total_cost_usd": 0,
            "errors": ["No conversation found with session ID: abc-123"],
        })
        with mock.patch("subprocess.run", return_value=_mock_proc(
            stdout=result_event + "\n", stderr="", returncode=1
        )):
            r = invoke_claude("hello", session_id="s1")

        assert "No conversation found" in r.stderr

    def test_result_event_errors_combined_with_proc_stderr(self):
        """When both proc.stderr and result errors exist, they are combined."""
        result_event = json.dumps({
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "result": "", "session_id": "s1",
            "num_turns": 0, "usage": {}, "total_cost_usd": 0,
            "errors": ["Some error from result"],
        })
        with mock.patch("subprocess.run", return_value=_mock_proc(
            stdout=result_event + "\n", stderr="proc stderr text", returncode=1
        )):
            r = invoke_claude("hello", session_id="s1")

        assert "proc stderr text" in r.stderr
        assert "Some error from result" in r.stderr

    def test_no_conversation_found_not_logged_to_structured(self):
        """Resume-first 'No conversation found' failures are suppressed from structured log."""
        result_event = json.dumps({
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "result": "", "session_id": "s1",
            "num_turns": 0, "usage": {}, "total_cost_usd": 0,
            "errors": ["No conversation found with session ID: abc-123"],
        })
        with mock.patch("subprocess.run", return_value=_mock_proc(
            stdout=result_event + "\n", stderr="", returncode=1
        )), mock.patch("claude_wrapper.log_event") as mock_log:
            invoke_claude("hello", session_id="s1")

        mock_log.assert_not_called()

    def test_unwritable_log_dir_does_not_crash(self, monkeypatch):
        monkeypatch.setattr(cw, "LOG_DIR", Path("/proc/nonexistent/logs"))
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")):
            r = invoke_claude("hello")

        assert r.exit_code == 0  # didn't crash


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class TestLogging:
    """Per-invocation log files are created with correct naming and content."""

    def test_log_file_created(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("hello", caller="test")

        log_files = list(cw.LOG_DIR.glob("*.log"))
        assert len(log_files) == 1

    def test_log_filename_contains_caller(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("hello", caller="discord")

        log_files = list(cw.LOG_DIR.glob("*.log"))
        assert "discord" in log_files[0].name

    def test_log_filename_contains_session_id(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("hello", caller="test", session_id="sess-abc")

        log_files = list(cw.LOG_DIR.glob("*.log"))
        assert "sess-abc" in log_files[0].name

    def test_log_filename_no_session(self):
        # No result event → no session_id parsed
        init = json.dumps({"type": "system", "subtype": "init", "model": "opus"})
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=init)):
            invoke_claude("hello", caller="test")

        log_files = list(cw.LOG_DIR.glob("*.log"))
        assert "no-session" in log_files[0].name

    def test_log_content_has_caller(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("hello", caller="cron-weather")

        content = list(cw.LOG_DIR.glob("*.log"))[0].read_text()
        assert "caller: cron-weather" in content

    def test_log_content_has_prompt(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("what is the weather?", caller="test")

        content = list(cw.LOG_DIR.glob("*.log"))[0].read_text()
        assert "what is the weather?" in content

    def test_log_content_has_stdout(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("hello", caller="test")

        content = list(cw.LOG_DIR.glob("*.log"))[0].read_text()
        assert "Hello world" in content

    def test_log_content_has_stderr(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}", stderr="warn!")):
            invoke_claude("hello", caller="test")

        content = list(cw.LOG_DIR.glob("*.log"))[0].read_text()
        assert "warn!" in content

    def test_log_content_has_exit_code(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}", returncode=1)):
            invoke_claude("hello", caller="test")

        content = list(cw.LOG_DIR.glob("*.log"))[0].read_text()
        assert "exit_code: 1" in content

    def test_log_content_has_duration(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")):
            invoke_claude("hello", caller="test")

        content = list(cw.LOG_DIR.glob("*.log"))[0].read_text()
        assert "duration:" in content

    def test_log_content_has_timestamps(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}")):
            invoke_claude("hello", caller="test")

        content = list(cw.LOG_DIR.glob("*.log"))[0].read_text()
        assert "start:" in content
        assert "end:" in content

    def test_error_paths_also_log(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            invoke_claude("hello", caller="err-test")

        log_files = list(cw.LOG_DIR.glob("*.log"))
        assert len(log_files) == 1
        assert "err-test" in log_files[0].name


# ---------------------------------------------------------------------------
# CLI interface (argparse)
# ---------------------------------------------------------------------------

class TestCLI:
    """The CLI parses arguments and produces correct JSON output."""

    def _run_cli(self, argv, proc_stdout="{}", proc_returncode=0):
        """Helper: run main() with mocked argv and subprocess, capture stdout."""
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=proc_stdout, returncode=proc_returncode)) as m, \
             mock.patch("sys.argv", ["claude_wrapper.py"] + argv):
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                cw.main()
            except SystemExit:
                pass
            finally:
                sys.stdout = old
            return m, buf.getvalue()

    def test_basic_invocation(self):
        _, output = self._run_cli(["hello"], proc_stdout=SAMPLE_STREAM_OUTPUT)
        data = json.loads(output)
        assert data["result"] == "Hello world"
        assert data["session_id"] == "sess-abc"

    def test_caller_flag(self):
        m, _ = self._run_cli(["--caller", "cron-test", "hello"])
        # Caller doesn't affect the subprocess command, but check it doesn't crash
        assert m.called

    def test_session_flag(self):
        m, _ = self._run_cli(["--session", "sess-123", "hello"])
        cmd = m.call_args[0][0]
        assert cmd[cmd.index("--resume") + 1] == "sess-123"

    def test_model_flag(self):
        m, _ = self._run_cli(["--model", "haiku", "hello"])
        cmd = m.call_args[0][0]
        assert cmd[cmd.index("--model") + 1] == "haiku"

    def test_allowed_tools_flag(self):
        m, _ = self._run_cli(["--allowed-tools", "Bash,Read", "hello"])
        cmd = m.call_args[0][0]
        assert cmd[cmd.index("--allowedTools") + 1] == "Bash,Read"

    def test_append_system_prompt_flag(self):
        with mock.patch.object(cw, "_load_user_memory", return_value=None), \
             mock.patch.object(cw, "_load_personality", return_value=None):
            m, _ = self._run_cli(["--append-system-prompt", "Be brief", "hello"])
        cmd = m.call_args[0][0]
        assert cmd[cmd.index("--append-system-prompt") + 1] == "Be brief"

    def test_no_skip_permissions_flag(self):
        m, _ = self._run_cli(["--no-skip-permissions", "hello"])
        cmd = m.call_args[0][0]
        assert "--dangerously-skip-permissions" not in cmd

    def test_max_turns_flag(self):
        m, _ = self._run_cli(["--max-turns", "3", "hello"])
        cmd = m.call_args[0][0]
        assert cmd[cmd.index("--max-turns") + 1] == "3"

    def test_max_budget_flag(self):
        m, _ = self._run_cli(["--max-budget-usd", "1.5", "hello"])
        cmd = m.call_args[0][0]
        assert cmd[cmd.index("--max-budget-usd") + 1] == "1.5"

    def test_exit_code_propagated(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="{}", returncode=1)), \
             mock.patch("sys.argv", ["claude_wrapper.py", "hello"]):
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            with pytest.raises(SystemExit) as exc_info:
                cw.main()
            sys.stdout = old
            assert exc_info.value.code == 1

    def test_cli_output_is_valid_json(self):
        _, output = self._run_cli(["hello"], proc_stdout=SAMPLE_STREAM_OUTPUT)
        data = json.loads(output)
        assert "result" in data
        assert "session_id" in data
        assert "exit_code" in data
        assert "duration" in data
        assert "usage" in data
        assert "model" in data
        assert "stderr" in data


# ---------------------------------------------------------------------------
# Python API and CLI produce identical logging
# ---------------------------------------------------------------------------

class TestAPICLILoggingParity:
    """Both Python API and CLI paths produce log files."""

    def test_python_api_creates_log(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("hello", caller="api-test")

        log_files = list(cw.LOG_DIR.glob("*.log"))
        assert len(log_files) == 1

    def test_cli_creates_log(self):
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)), \
             mock.patch("sys.argv", ["claude_wrapper.py", "--caller", "cli-test", "hello"]):
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                cw.main()
            except SystemExit:
                pass
            finally:
                sys.stdout = old

        log_files = list(cw.LOG_DIR.glob("*.log"))
        assert len(log_files) == 1

    def test_log_content_identical_structure(self):
        """Both paths produce logs with the same sections."""
        required_sections = ["caller:", "start:", "end:", "duration:", "session_id:",
                             "exit_code:", "=== PROMPT ===", "=== STDOUT ===", "=== STDERR ==="]

        # Python API
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)):
            invoke_claude("hello", caller="api")
        api_log = list(cw.LOG_DIR.glob("*.log"))[0].read_text()

        # Clean for next call
        for f in cw.LOG_DIR.glob("*.log"):
            f.unlink()

        # CLI
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout=SAMPLE_STREAM_OUTPUT)), \
             mock.patch("sys.argv", ["claude_wrapper.py", "--caller", "cli", "hello"]):
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                cw.main()
            except SystemExit:
                pass
            finally:
                sys.stdout = old
        cli_log = list(cw.LOG_DIR.glob("*.log"))[0].read_text()

        for section in required_sections:
            assert section in api_log, f"API log missing: {section}"
            assert section in cli_log, f"CLI log missing: {section}"
