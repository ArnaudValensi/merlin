"""Tests for lib/engines/opencode.py — OpenCodeEngine."""

import subprocess
from unittest import mock


from lib.engine import AgentResult, get_engine
from lib.engines.opencode import OpenCodeEngine, _format_history


def _mock_proc(stdout="", stderr="", returncode=0):
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# Engine properties
# ---------------------------------------------------------------------------


class TestOpenCodeEngineProperties:
    def test_name(self):
        assert OpenCodeEngine().name == "opencode"

    def test_context_window(self):
        assert OpenCodeEngine().context_window == 200_000

    def test_supports_tool_use(self):
        assert OpenCodeEngine().supports_tool_use is True

    def test_supports_system_prompt(self):
        assert OpenCodeEngine().supports_system_prompt is False

    def test_supports_streaming(self):
        assert OpenCodeEngine().supports_streaming is False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_registered(self):
        engine = get_engine("opencode")
        assert engine.name == "opencode"


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_binary_found(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/opencode")
        assert OpenCodeEngine().validate() is None

    def test_binary_missing(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: None)
        error = OpenCodeEngine().validate()
        assert error is not None
        assert "not found" in error


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


class TestCommandConstruction:
    def test_basic_command(self):
        engine = OpenCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="Hello")) as m:
            engine.invoke("test prompt")
            cmd = m.call_args[0][0]

        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        assert "test prompt" in cmd[-1]

    def test_model_flag(self):
        engine = OpenCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="Hello")) as m:
            engine.invoke("test", model="anthropic/claude-sonnet")
            cmd = m.call_args[0][0]

        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "anthropic/claude-sonnet"

    def test_system_prompt_in_text(self):
        """System prompt is prepended to the prompt text (no native flag)."""
        engine = OpenCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="ok")) as m:
            engine.invoke("hello", system_prompt="Be concise")
            cmd = m.call_args[0][0]

        prompt = cmd[-1]
        assert "Be concise" in prompt
        assert "hello" in prompt

    def test_history_in_prompt(self):
        engine = OpenCodeEngine()
        history = [
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": "Reply"},
        ]
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="ok")) as m:
            engine.invoke("Follow up", history=history)
            cmd = m.call_args[0][0]

        prompt = cmd[-1]
        assert "[User]: First" in prompt
        assert "[Assistant]: Reply" in prompt
        assert "Follow up" in prompt

    def test_no_resume_flags(self):
        """OpenCode never uses --resume or --session-id."""
        engine = OpenCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="ok")) as m:
            engine.invoke("test", session_id="abc-123")
            cmd = m.call_args[0][0]

        assert "--resume" not in cmd
        assert "--session-id" not in cmd


# ---------------------------------------------------------------------------
# History formatting
# ---------------------------------------------------------------------------


class TestHistoryFormatting:
    def test_empty(self):
        assert _format_history([]) == ""

    def test_user_and_assistant(self):
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = _format_history(history)
        assert "[User]: Hello" in result
        assert "[Assistant]: Hi" in result

    def test_skips_system(self):
        history = [{"role": "system", "content": "hidden"}]
        assert _format_history(history) == ""


# ---------------------------------------------------------------------------
# Invoke result
# ---------------------------------------------------------------------------


class TestInvokeResult:
    def test_success(self):
        engine = OpenCodeEngine()
        with mock.patch(
            "subprocess.run", return_value=_mock_proc(stdout="The answer is 42")
        ):
            r = engine.invoke("question")
        assert isinstance(r, AgentResult)
        assert r.content == "The answer is 42"
        assert r.exit_code == 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_binary_not_found(self):
        engine = OpenCodeEngine()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            r = engine.invoke("hello")
        assert r.exit_code == 127
        assert "command not found" in r.stderr

    def test_timeout(self):
        engine = OpenCodeEngine()
        with mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("opencode", 10)
        ):
            r = engine.invoke("hello", timeout=10)
        assert r.exit_code == 124
        assert "timed out" in r.stderr


class TestAgentsSkillsSync:
    def test_invoke_syncs_agents_skills(self, tmp_path, monkeypatch):
        from lib import skills

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        # One canonical skill
        skill_dir = tmp_path / "src" / "jobs"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: jobs\ndescription: X.\n---\n")
        canonical = skills.canonical_dir()
        canonical.mkdir(parents=True, exist_ok=True)
        (canonical / "jobs").symlink_to(skill_dir)

        engine = OpenCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="ok")):
            engine.invoke("hello")

        link = home / ".agents" / "skills" / "jobs"
        assert link.is_symlink()
        assert (link / "SKILL.md").is_file()


class TestCwdHandling:
    def test_explicit_cwd_passed_to_subprocess(self, tmp_path):
        engine = OpenCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="ok")) as m:
            engine.invoke("hello", cwd=tmp_path)
        assert m.call_args.kwargs["cwd"] == tmp_path

    def test_no_cwd_means_inherit_not_app_dir(self):
        engine = OpenCodeEngine()
        with mock.patch("subprocess.run", return_value=_mock_proc(stdout="ok")) as m:
            engine.invoke("hello")
        assert m.call_args.kwargs["cwd"] is None
