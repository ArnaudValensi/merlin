"""Tests for merlin_bot.py — Discord bot listener with thread-based sessions."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import merlin_bot as merlin
import session_registry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DEFAULT_CHANNEL_ID = 1234567890123456789


def make_message(
    *,
    author_name: str = "TestUser",
    author_bot: bool = False,
    channel_id: int = DEFAULT_CHANNEL_ID,
    message_id: int = 999888777,
    content: str = "Hello Merlin!",
    is_thread: bool = False,
    thread_id: int | None = None,
    parent_channel_id: int | None = None,
    voice: bool = False,
    attachments: list | None = None,
) -> MagicMock:
    """Create a fake discord.Message.

    If is_thread=True, message.channel is a Thread with parent_id.
    Otherwise, message.channel is a regular TextChannel.
    If voice=True, message.flags.voice is set and a default .ogg attachment is added.
    """
    msg = MagicMock()
    msg.author.display_name = author_name
    msg.author.bot = author_bot
    msg.id = message_id
    msg.content = content
    msg.type = discord.MessageType.default
    msg.add_reaction = AsyncMock()
    msg.remove_reaction = AsyncMock()

    # Flags
    msg.flags = MagicMock()
    msg.flags.voice = voice

    # Attachments
    if attachments is not None:
        msg.attachments = attachments
    elif voice:
        att = MagicMock()
        att.filename = "voice-message.ogg"
        att.read = AsyncMock(return_value=b"fake-ogg-data")
        msg.attachments = [att]
    else:
        msg.attachments = []

    if is_thread:
        msg.channel = MagicMock(spec=discord.Thread)
        msg.channel.id = thread_id or 88888888
        msg.channel.parent_id = parent_channel_id or channel_id
    else:
        msg.channel = MagicMock(spec=discord.TextChannel)
        msg.channel.id = channel_id

    return msg


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """Reset module state and redirect registry to tmp for every test."""
    # Clean module state
    merlin._new_sessions.clear()
    merlin._channel_locks.clear()
    merlin.DISCORD_CHANNEL_IDS = {str(DEFAULT_CHANNEL_ID)}

    # Redirect session registry to tmp
    monkeypatch.setattr(session_registry, "DATA_DIR", tmp_path)
    monkeypatch.setattr(session_registry, "REGISTRY_PATH", tmp_path / "session_registry.json")


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_contains_author(self):
        msg = make_message(author_name="Alice")
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222")
        assert '"Alice"' in prompt

    def test_contains_thread_id(self):
        msg = make_message()
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222")
        assert "thread 111" in prompt

    def test_contains_channel_id(self):
        msg = make_message()
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222")
        assert "channel 222" in prompt

    def test_contains_message_id(self):
        msg = make_message(message_id=67890)
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222")
        assert "67890" in prompt

    def test_contains_content(self):
        msg = make_message(content="What is the weather?")
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222")
        assert "What is the weather?" in prompt

    def test_format_matches_spec(self):
        msg = make_message(
            author_name="Bob",
            message_id=222,
            content="hi",
        )
        prompt = merlin.build_prompt(msg, thread_id="789", parent_id="123")
        assert prompt == (
            '[Discord message from "Bob" in thread 789,'
            " channel 123, message ID 222]\n"
            "hi"
        )

    def test_new_thread_tag_prepended(self):
        msg = make_message(content="hello")
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222", is_new_thread=True)
        assert prompt.startswith("[New thread]\n")
        assert "[Discord message from" in prompt

    def test_no_new_thread_tag_by_default(self):
        msg = make_message(content="hello")
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222")
        assert "[New thread]" not in prompt

    def test_new_thread_tag_with_voice(self):
        msg = make_message(voice=True, content="")
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222", transcription="test", is_new_thread=True)
        assert prompt.startswith("[New thread]\n")
        assert "[Discord voice message" in prompt

    def test_no_new_thread_tag_with_voice_by_default(self):
        msg = make_message(voice=True, content="")
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222", transcription="test")
        assert "[New thread]" not in prompt


# ---------------------------------------------------------------------------
# session_id helpers
# ---------------------------------------------------------------------------


class TestSessionId:
    def test_channel_deterministic(self):
        sid = merlin.session_id_for_channel("123")
        assert sid == merlin.session_id_for_channel("123")
        import uuid
        uuid.UUID(sid)

    def test_thread_deterministic(self):
        sid = merlin.session_id_for_thread("123")
        assert sid == merlin.session_id_for_thread("123")
        import uuid
        uuid.UUID(sid)

    def test_thread_differs_from_channel(self):
        """Same numeric ID should produce different sessions for thread vs channel."""
        assert merlin.session_id_for_channel("123") != merlin.session_id_for_thread("123")

    def test_integer_input(self):
        assert merlin.session_id_for_channel(123) == merlin.session_id_for_channel("123")
        assert merlin.session_id_for_thread(123) == merlin.session_id_for_thread("123")

    def test_different_threads_different_sessions(self):
        assert merlin.session_id_for_thread("111") != merlin.session_id_for_thread("222")


# ---------------------------------------------------------------------------
# _resolve_allowed_channel
# ---------------------------------------------------------------------------


class TestResolveAllowedChannel:
    def test_allowed_channel(self):
        msg = make_message(channel_id=DEFAULT_CHANNEL_ID)
        assert merlin._resolve_allowed_channel(msg) == str(DEFAULT_CHANNEL_ID)

    def test_disallowed_channel(self):
        msg = make_message(channel_id=9999999)
        assert merlin._resolve_allowed_channel(msg) is None

    def test_thread_in_allowed_parent(self):
        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        assert merlin._resolve_allowed_channel(msg) == str(DEFAULT_CHANNEL_ID)

    def test_thread_in_disallowed_parent(self):
        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=9999999,
        )
        assert merlin._resolve_allowed_channel(msg) is None


# ---------------------------------------------------------------------------
# on_message behaviour
# ---------------------------------------------------------------------------


class TestOnMessage:
    """Test the on_message event handler."""

    @patch("merlin_bot.invoke_claude")
    def test_ignores_bot_messages(self, mock_invoke):
        msg = make_message(author_bot=True)
        asyncio.run(merlin.on_message(msg))
        mock_invoke.assert_not_called()

    @patch("merlin_bot.invoke_claude")
    def test_ignores_unconfigured_channel(self, mock_invoke):
        msg = make_message(channel_id=9999999)
        asyncio.run(merlin.on_message(msg))
        mock_invoke.assert_not_called()

    @patch("merlin_bot.invoke_claude")
    def test_ignores_thread_in_unconfigured_channel(self, mock_invoke):
        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=9999999,
        )
        asyncio.run(merlin.on_message(msg))
        mock_invoke.assert_not_called()

    @patch("merlin_bot.create_thread_from_message")
    @patch("merlin_bot.load_token", return_value="fake-token")
    @patch("merlin_bot.invoke_claude")
    def test_channel_message_creates_thread(self, mock_invoke, mock_token, mock_create_thread):
        mock_create_thread.return_value = {"id": "77777"}
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")
        msg = make_message(channel_id=DEFAULT_CHANNEL_ID)
        asyncio.run(merlin.on_message(msg))

        mock_create_thread.assert_called_once()
        mock_invoke.assert_called_once()

        # Verify session is registered
        assert session_registry.get_thread_session("77777") is not None

    @patch("merlin_bot.create_thread_from_message")
    @patch("merlin_bot.load_token", return_value="fake-token")
    @patch("merlin_bot.invoke_claude")
    def test_channel_message_prompt_has_thread_id(self, mock_invoke, mock_token, mock_create_thread):
        mock_create_thread.return_value = {"id": "77777"}
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")
        msg = make_message(channel_id=DEFAULT_CHANNEL_ID, content="test content")
        asyncio.run(merlin.on_message(msg))

        prompt_arg = mock_invoke.call_args[0][0]
        assert "thread 77777" in prompt_arg
        assert f"channel {DEFAULT_CHANNEL_ID}" in prompt_arg
        assert "test content" in prompt_arg

    @patch("merlin_bot.create_thread_from_message")
    @patch("merlin_bot.load_token", return_value="fake-token")
    @patch("merlin_bot.invoke_claude")
    def test_channel_message_prompt_has_new_thread_tag(self, mock_invoke, mock_token, mock_create_thread):
        mock_create_thread.return_value = {"id": "77777"}
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")
        msg = make_message(channel_id=DEFAULT_CHANNEL_ID, content="test content")
        asyncio.run(merlin.on_message(msg))

        prompt_arg = mock_invoke.call_args[0][0]
        assert prompt_arg.startswith("[New thread]\n")

    @patch("merlin_bot.invoke_claude")
    def test_thread_message_prompt_has_no_new_thread_tag(self, mock_invoke):
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")
        session_registry.set_thread_session("88888888", "existing-session-id")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        asyncio.run(merlin.on_message(msg))

        prompt_arg = mock_invoke.call_args[0][0]
        assert "[New thread]" not in prompt_arg

    @patch("merlin_bot.invoke_claude")
    def test_thread_message_uses_registered_session(self, mock_invoke):
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")
        session_registry.set_thread_session("88888888", "existing-session-id")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        asyncio.run(merlin.on_message(msg))

        kwargs = mock_invoke.call_args[1]
        assert kwargs["session_id"] == "existing-session-id"

    @patch("merlin_bot.invoke_claude")
    def test_thread_message_creates_deterministic_session_if_unregistered(self, mock_invoke):
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        asyncio.run(merlin.on_message(msg))

        expected_session = merlin.session_id_for_thread("88888888")
        kwargs = mock_invoke.call_args[1]
        assert kwargs["session_id"] == expected_session

        # Should also be registered now
        assert session_registry.get_thread_session("88888888") == expected_session

    @patch("merlin_bot.invoke_claude")
    def test_cron_continuation_via_message_registry(self, mock_invoke):
        """Threading on a cron message should resume the cron session."""
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")
        # Simulate cron having registered a message
        session_registry.set_message_session("88888888", "cron-session-abc")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,  # Thread ID == starter message ID
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        asyncio.run(merlin.on_message(msg))

        kwargs = mock_invoke.call_args[1]
        assert kwargs["session_id"] == "cron-session-abc"

        # Should be registered as thread session too
        assert session_registry.get_thread_session("88888888") == "cron-session-abc"

    @patch("merlin_bot.invoke_claude")
    def test_resume_first_strategy(self, mock_invoke):
        """Default: try --resume, fall back to --session-id."""
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")
        session_registry.set_thread_session("88888888", "some-session")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        asyncio.run(merlin.on_message(msg))

        kwargs = mock_invoke.call_args[1]
        assert kwargs["resume"] is True

    @patch("merlin_bot.invoke_claude")
    def test_resume_fallback_creates_session(self, mock_invoke):
        """If --resume fails, retry with --session-id."""
        fail_result = MagicMock(exit_code=1, session_id=None, stderr="No conversation found with session ID: xxx")
        ok_result = MagicMock(exit_code=0, session_id="s1", stderr="")
        mock_invoke.side_effect = [fail_result, ok_result]
        session_registry.set_thread_session("88888888", "some-session")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        asyncio.run(merlin.on_message(msg))

        assert mock_invoke.call_count == 2
        assert mock_invoke.call_args_list[0][1]["resume"] is True
        assert mock_invoke.call_args_list[1][1]["resume"] is False

    @patch("merlin_bot.invoke_claude")
    def test_error_does_not_crash(self, mock_invoke):
        mock_invoke.side_effect = RuntimeError("boom")
        session_registry.set_thread_session("88888888", "some-session")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        # Should not raise
        asyncio.run(merlin.on_message(msg))

    @patch("merlin_bot.invoke_claude")
    def test_success_reaction(self, mock_invoke):
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")
        session_registry.set_thread_session("88888888", "some-session")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        asyncio.run(merlin.on_message(msg))

        msg.add_reaction.assert_any_call("\N{THINKING FACE}")
        msg.remove_reaction.assert_called_once_with("\N{THINKING FACE}", merlin.client.user)
        msg.add_reaction.assert_any_call("\N{WHITE HEAVY CHECK MARK}")

    @patch("merlin_bot.invoke_claude")
    def test_error_reaction_on_nonzero_exit(self, mock_invoke):
        mock_invoke.return_value = MagicMock(exit_code=1, session_id="s1", stderr="fail")
        session_registry.set_thread_session("88888888", "some-session")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        asyncio.run(merlin.on_message(msg))

        msg.add_reaction.assert_any_call("\N{THINKING FACE}")
        msg.remove_reaction.assert_called_once_with("\N{THINKING FACE}", merlin.client.user)
        msg.add_reaction.assert_any_call("\N{CROSS MARK}")

    @patch("merlin_bot.invoke_claude")
    def test_error_reaction_on_exception(self, mock_invoke):
        mock_invoke.side_effect = RuntimeError("boom")
        session_registry.set_thread_session("88888888", "some-session")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        asyncio.run(merlin.on_message(msg))

        msg.add_reaction.assert_any_call("\N{THINKING FACE}")
        msg.remove_reaction.assert_called_once_with("\N{THINKING FACE}", merlin.client.user)
        msg.add_reaction.assert_any_call("\N{CROSS MARK}")

    @patch("merlin_bot.invoke_claude")
    def test_session_resolve_failure_gives_error_reaction(self, mock_invoke):
        """If thread/session resolution fails, show ❌ without invoking Claude."""
        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        # Make registry fail
        with patch("merlin_bot.get_thread_session", side_effect=RuntimeError("disk error")):
            asyncio.run(merlin.on_message(msg))

        mock_invoke.assert_not_called()
        msg.add_reaction.assert_any_call("\N{CROSS MARK}")


# ---------------------------------------------------------------------------
# build_prompt — voice messages
# ---------------------------------------------------------------------------


class TestBuildPromptVoice:
    def test_voice_prompt_header(self):
        msg = make_message(author_name="Alice", voice=True, content="")
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222", transcription="hello world")
        assert "[Discord voice message" in prompt
        assert '"Alice"' in prompt

    def test_voice_prompt_contains_transcription(self):
        msg = make_message(voice=True, content="")
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222", transcription="check the cron jobs")
        assert "[Transcribed audio]: check the cron jobs" in prompt

    def test_voice_prompt_with_text_content(self):
        """Rare case: voice message with accompanying text."""
        msg = make_message(voice=True, content="some text too")
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222", transcription="audio text")
        assert "[Transcribed audio]: audio text" in prompt
        assert "some text too" in prompt

    def test_no_transcription_gives_regular_prompt(self):
        msg = make_message(content="normal message")
        prompt = merlin.build_prompt(msg, thread_id="111", parent_id="222", transcription=None)
        assert "[Discord message from" in prompt
        assert "[Transcribed audio]" not in prompt

    def test_voice_prompt_format(self):
        msg = make_message(author_name="Bob", message_id=555, voice=True, content="")
        prompt = merlin.build_prompt(msg, thread_id="789", parent_id="123", transcription="hi there")
        assert prompt == (
            '[Discord voice message from "Bob" in thread 789,'
            " channel 123, message ID 555]\n"
            "[Transcribed audio]: hi there"
        )


# ---------------------------------------------------------------------------
# Voice message handling in on_message
# ---------------------------------------------------------------------------


class TestVoiceMessages:
    @patch("merlin_bot.transcribe", return_value="transcribed text here")
    @patch("merlin_bot.invoke_claude")
    def test_voice_message_transcribed_and_sent(self, mock_invoke, mock_transcribe):
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")
        session_registry.set_thread_session("88888888", "some-session")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
            voice=True,
            content="",
        )
        asyncio.run(merlin.on_message(msg))

        mock_transcribe.assert_called_once()
        prompt_arg = mock_invoke.call_args[0][0]
        assert "[Transcribed audio]: transcribed text here" in prompt_arg
        assert "[Discord voice message" in prompt_arg

    @patch("merlin_bot.transcribe", side_effect=RuntimeError("model failed"))
    @patch("merlin_bot.invoke_claude")
    def test_transcription_failure_still_sends_prompt(self, mock_invoke, mock_transcribe):
        """If transcription fails, send a failure notice instead of crashing."""
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")
        session_registry.set_thread_session("88888888", "some-session")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
            voice=True,
            content="",
        )
        asyncio.run(merlin.on_message(msg))

        mock_invoke.assert_called_once()
        prompt_arg = mock_invoke.call_args[0][0]
        assert "[transcription failed]" in prompt_arg

    @patch("merlin_bot.transcribe")
    @patch("merlin_bot.invoke_claude")
    def test_non_voice_message_skips_transcription(self, mock_invoke, mock_transcribe):
        mock_invoke.return_value = MagicMock(exit_code=0, session_id="s1", stderr="")
        session_registry.set_thread_session("88888888", "some-session")

        msg = make_message(
            is_thread=True,
            thread_id=88888888,
            parent_channel_id=DEFAULT_CHANNEL_ID,
        )
        asyncio.run(merlin.on_message(msg))

        mock_transcribe.assert_not_called()
        prompt_arg = mock_invoke.call_args[0][0]
        assert "[Discord message from" in prompt_arg


# ---------------------------------------------------------------------------
# Plugin interface
# ---------------------------------------------------------------------------


class TestPluginInterface:
    """Verify merlin_bot.py exports the plugin interface main.py expects."""

    def test_router_is_api_router(self):
        from fastapi import APIRouter
        assert isinstance(merlin.router, APIRouter)

    def test_nav_items_is_list(self):
        assert isinstance(merlin.NAV_ITEMS, list)
        assert len(merlin.NAV_ITEMS) > 0
        for item in merlin.NAV_ITEMS:
            assert "url" in item
            assert "label" in item

    def test_static_dir(self):
        # STATIC_DIR is None (uses root statics)
        assert merlin.STATIC_DIR is None

    def test_start_is_coroutine_function(self):
        import inspect
        assert inspect.iscoroutinefunction(merlin.start)

    def test_on_tunnel_url_is_coroutine_function(self):
        import inspect
        assert inspect.iscoroutinefunction(merlin.on_tunnel_url)

    def test_validate_is_callable(self):
        assert callable(merlin.validate)
