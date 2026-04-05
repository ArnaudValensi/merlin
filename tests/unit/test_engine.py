"""Tests for lib/engine.py — AgentEngine abstraction, registry, and invoke()."""

import json
import shutil
from pathlib import Path
from unittest import mock

import pytest

from lib.engine import (
    AgentEngine,
    AgentResult,
    _build_system_prompt,
    _load_personality,
    _load_user_context,
    _registry,
    get_engine,
    invoke,
    register_engine,
)


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------


class TestAgentResult:
    """AgentResult dataclass fields and defaults."""

    def test_fields(self):
        r = AgentResult(
            content="ok", exit_code=0, duration=1.5,
            stderr="", usage={"input_tokens": 10}, model="opus",
            cost_usd=0.01, raw_output="raw", session_id="s1",
        )
        assert r.content == "ok"
        assert r.exit_code == 0
        assert r.duration == 1.5
        assert r.usage == {"input_tokens": 10}
        assert r.model == "opus"
        assert r.cost_usd == 0.01
        assert r.raw_output == "raw"
        assert r.session_id == "s1"

    def test_defaults(self):
        r = AgentResult(content="", exit_code=0, duration=0)
        assert r.stderr == ""
        assert r.usage == {}
        assert r.model is None
        assert r.cost_usd is None
        assert r.raw_output == ""
        assert r.tool_calls == []
        assert r.session_id is None

    def test_result_property_alias(self):
        """The .result property returns .content for backward compatibility."""
        r = AgentResult(content="hello", exit_code=0, duration=0)
        assert r.result == "hello"


# ---------------------------------------------------------------------------
# AgentEngine ABC
# ---------------------------------------------------------------------------


class TestAgentEngineABC:
    """AgentEngine cannot be instantiated directly."""

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            AgentEngine()  # type: ignore[abstract]

    def test_concrete_subclass(self):
        class DummyEngine(AgentEngine):
            name = "dummy"
            context_window = 100_000

            def invoke(self, prompt, **kwargs):
                return AgentResult(content="hi", exit_code=0, duration=0.1)

        engine = DummyEngine()
        assert engine.name == "dummy"
        assert engine.context_window == 100_000
        assert engine.validate() is None
        assert engine.supports_tool_use is True
        assert engine.supports_system_prompt is True
        assert engine.supports_streaming is False

        result = engine.invoke("test")
        assert result.content == "hi"


# ---------------------------------------------------------------------------
# Engine registry
# ---------------------------------------------------------------------------


class TestEngineRegistry:
    """get_engine() returns the correct engine based on config."""

    def test_default_is_claude_code(self):
        engine = get_engine()
        assert engine.name == "claude-code"

    def test_explicit_claude_code(self):
        engine = get_engine("claude-code")
        assert engine.name == "claude-code"

    def test_unknown_engine_raises(self):
        with pytest.raises(ValueError, match="Unknown engine"):
            get_engine("nonexistent-engine")

    def test_env_var_override(self, monkeypatch):
        """AGENT_ENGINE env var selects engine."""
        class FakeEngine(AgentEngine):
            name = "fake"
            context_window = 50_000

            def invoke(self, prompt, **kwargs):
                return AgentResult(content="", exit_code=0, duration=0)

        register_engine("fake", FakeEngine)
        try:
            monkeypatch.setenv("AGENT_ENGINE", "fake")
            engine = get_engine()
            assert engine.name == "fake"
        finally:
            _registry.pop("fake", None)

    def test_register_and_get(self):
        class TestEngine(AgentEngine):
            name = "test-engine"
            context_window = 10_000

            def invoke(self, prompt, **kwargs):
                return AgentResult(content="", exit_code=0, duration=0)

        register_engine("test-engine", TestEngine)
        try:
            engine = get_engine("test-engine")
            assert engine.name == "test-engine"
        finally:
            _registry.pop("test-engine", None)


# ---------------------------------------------------------------------------
# Personality / user context loading
# ---------------------------------------------------------------------------


class TestPersonalityLoading:
    """Personality and user context load from new paths with legacy fallback."""

    def test_personality_from_new_path(self, tmp_path, monkeypatch):
        import lib.engine as eng
        monkeypatch.setattr(eng.paths, "merlin_home", lambda: tmp_path)
        (tmp_path / "personality.md").write_text("Be awesome")
        assert _load_personality() == "Be awesome"

    def test_personality_fallback_to_legacy(self, tmp_path, monkeypatch):
        import lib.engine as eng
        monkeypatch.setattr(eng.paths, "merlin_home", lambda: tmp_path)
        (tmp_path / "merlin-bot").mkdir()
        (tmp_path / "merlin-bot" / "personality.md").write_text("Legacy")
        assert _load_personality() == "Legacy"

    def test_personality_missing_returns_none(self, tmp_path, monkeypatch):
        import lib.engine as eng
        monkeypatch.setattr(eng.paths, "merlin_home", lambda: tmp_path)
        assert _load_personality() is None

    def test_user_context_from_new_path(self, tmp_path, monkeypatch):
        import lib.engine as eng
        monkeypatch.setattr(eng.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(eng.paths, "notes_dir", lambda: tmp_path / "notes")
        (tmp_path / "user.md").write_text("User is a dev")
        result = _load_user_context()
        assert "User is a dev" in result
        assert "# User Memory" in result

    def test_user_context_missing_returns_none(self, tmp_path, monkeypatch):
        import lib.engine as eng
        monkeypatch.setattr(eng.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(eng.paths, "notes_dir", lambda: tmp_path / "notes")
        assert _load_user_context() is None


class TestBuildSystemPrompt:
    """_build_system_prompt combines personality, user context, and extras."""

    def test_with_personality_and_context(self, tmp_path, monkeypatch):
        import lib.engine as eng
        monkeypatch.setattr(eng.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(eng.paths, "notes_dir", lambda: tmp_path / "notes")
        (tmp_path / "personality.md").write_text("Be cool")
        (tmp_path / "user.md").write_text("User is a dev")

        result = _build_system_prompt()
        assert "Be cool" in result
        assert "User is a dev" in result

    def test_with_extra_file(self, tmp_path, monkeypatch):
        import lib.engine as eng
        monkeypatch.setattr(eng.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(eng.paths, "notes_dir", lambda: tmp_path / "notes")
        extra = tmp_path / "extra.md"
        extra.write_text("Extra rule")

        result = _build_system_prompt(extra_system_prompts=[extra])
        assert "Extra rule" in result

    def test_with_append_system_prompt(self, tmp_path, monkeypatch):
        import lib.engine as eng
        monkeypatch.setattr(eng.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(eng.paths, "notes_dir", lambda: tmp_path / "notes")

        result = _build_system_prompt(append_system_prompt="Be concise")
        assert result == "Be concise"

    def test_missing_extra_file_skipped(self, tmp_path, monkeypatch):
        import lib.engine as eng
        monkeypatch.setattr(eng.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(eng.paths, "notes_dir", lambda: tmp_path / "notes")

        result = _build_system_prompt(extra_system_prompts=[tmp_path / "nope.md"])
        assert result is None

    def test_empty_returns_none(self, tmp_path, monkeypatch):
        import lib.engine as eng
        monkeypatch.setattr(eng.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(eng.paths, "notes_dir", lambda: tmp_path / "notes")

        result = _build_system_prompt()
        assert result is None


# ---------------------------------------------------------------------------
# invoke() wrapper
# ---------------------------------------------------------------------------


class TestInvoke:
    """Top-level invoke() delegates to engine and logs."""

    @pytest.fixture(autouse=True)
    def _clean_dirs(self, tmp_path, monkeypatch):
        import lib.engine as eng
        monkeypatch.setattr(eng, "RAW_SESSION_DIR", tmp_path / "logs" / "sessions")
        monkeypatch.setattr(eng.paths, "merlin_home", lambda: tmp_path)
        monkeypatch.setattr(eng.paths, "notes_dir", lambda: tmp_path / "notes")

    def test_invoke_returns_agent_result(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(
            stdout="{}", stderr="", returncode=0
        )):
            result = invoke("hello", caller="test")
        assert isinstance(result, AgentResult)
        assert result.exit_code == 0

    def test_invoke_logs_structured_event(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(
            stdout="{}", stderr="", returncode=0
        )), mock.patch("lib.engine.log_event") as mock_log:
            invoke("hello", caller="test")
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args
        assert call_kwargs[0][0] == "invocation"

    def test_invoke_skips_resume_failure_log(self):
        """Don't log 'No conversation found' failures."""
        with mock.patch("subprocess.run", return_value=mock.Mock(
            stdout="{}", stderr="No conversation found for session abc",
            returncode=1,
        )), mock.patch("lib.engine.log_event") as mock_log:
            invoke("hello", caller="test")
        mock_log.assert_not_called()

    def test_invoke_passes_system_prompt(self, tmp_path):
        (tmp_path / "personality.md").write_text("Be nice")
        with mock.patch("subprocess.run", return_value=mock.Mock(
            stdout="{}", stderr="", returncode=0
        )) as mock_run:
            invoke("hello", caller="test")
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--append-system-prompt")
        assert "Be nice" in cmd[idx + 1]
