# /// script
# dependencies = ["pytest", "httpx", "python-dotenv"]
# ///
"""Tests for discord_send.py — all HTTP calls are mocked."""

import json
import os
import sys
import urllib.parse
from unittest import mock

import httpx
import pytest

import discord_send as ds


# ---------------------------------------------------------------------------
# chunk_message() — pure logic, no mocking needed
# ---------------------------------------------------------------------------

class TestChunkMessage:
    """chunk_message() splits text correctly."""

    def test_empty_string_returns_list_with_empty_string(self):
        assert ds.chunk_message("") == [""]

    def test_short_message_single_chunk(self):
        msg = "Hello, world!"
        result = ds.chunk_message(msg)
        assert result == [msg]

    def test_exact_2000_chars_single_chunk(self):
        msg = "a" * 2000
        result = ds.chunk_message(msg)
        assert result == [msg]

    def test_long_message_splits_at_newline_boundary(self):
        # Create a message where the first chunk should split at a newline
        line = "a" * 100 + "\n"  # 101 chars per line
        msg = line * 25  # 2525 chars total
        result = ds.chunk_message(msg, max_len=2000)
        assert len(result) == 2
        # First chunk should end at a newline boundary
        assert result[0].endswith("\n")
        assert len(result[0]) <= 2000

    def test_long_message_splits_at_space_when_no_newline(self):
        # No newlines — must split at space
        word = "abcdefghij "  # 11 chars per word+space
        msg = word * 200  # 2200 chars
        result = ds.chunk_message(msg, max_len=2000)
        assert len(result) == 2
        # First chunk should end at a space boundary
        assert result[0].endswith(" ")
        assert len(result[0]) <= 2000

    def test_long_message_hard_cuts_when_no_space_or_newline(self):
        msg = "a" * 3000  # No spaces, no newlines
        result = ds.chunk_message(msg, max_len=2000)
        assert len(result) == 2
        assert len(result[0]) == 2000
        assert len(result[1]) == 1000

    def test_multiple_chunks_for_very_long_message(self):
        msg = "a" * 5000
        result = ds.chunk_message(msg, max_len=2000)
        assert len(result) == 3
        assert "".join(result) == msg

    def test_newlines_preferred_over_spaces(self):
        # Build a message with both spaces and newlines before the limit
        # The split should happen at the newline, not the space
        part_a = "a" * 990 + "\n"   # 991 chars, ends with newline
        part_b = "b" * 500 + " "    # 501 chars, ends with space
        part_c = "c" * 600          # 600 chars
        msg = part_a + part_b + part_c  # 2092 total
        result = ds.chunk_message(msg, max_len=2000)
        assert len(result) == 2
        # The split should be at the newline inside the first 2000 chars
        # part_a + part_b = 1492, which is within 2000
        # So the newline at position 991 should be preferred
        # Actually let's check: within the first 2000 chars, the last newline is at pos 990
        # and the last space is at pos 1491. Since newlines are preferred, split at newline.
        assert result[0] == part_a


# ---------------------------------------------------------------------------
# Helper: mock httpx.Client
# ---------------------------------------------------------------------------

def _make_mock_response(status_code=200, json_body=None, content=b""):
    """Create a mock httpx.Response."""
    resp = mock.Mock(spec=httpx.Response)
    resp.status_code = status_code
    if json_body is not None:
        resp.json.return_value = json_body
        resp.content = json.dumps(json_body).encode()
    else:
        resp.content = content
        resp.json.side_effect = Exception("No JSON body")
    resp.text = content.decode() if isinstance(content, bytes) else str(content)
    return resp


def _mock_client_with_responses(*responses):
    """Return a context-manager mock for httpx.Client that returns given responses."""
    client_instance = mock.Mock()
    # Set up post to return responses in order
    client_instance.post.side_effect = list(responses) if len(responses) > 1 else None
    if len(responses) == 1:
        client_instance.post.return_value = responses[0]
    # Set up put similarly
    client_instance.put.side_effect = list(responses) if len(responses) > 1 else None
    if len(responses) == 1:
        client_instance.put.return_value = responses[0]
    return client_instance


# ---------------------------------------------------------------------------
# send_message() tests
# ---------------------------------------------------------------------------

class TestSendMessage:
    """send_message() sends correct HTTP requests."""

    def test_correct_url(self):
        resp = _make_mock_response(json_body={"id": "111", "channel_id": "chan1"})
        client = _mock_client_with_responses(resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.send_message("chan1", "hello", "tok123")

        url = client.post.call_args[1].get("url", client.post.call_args[0][0] if client.post.call_args[0] else None)
        if url is None:
            url = client.post.call_args[0][0]
        assert url == "https://discord.com/api/v10/channels/chan1/messages"

    def test_correct_headers(self):
        resp = _make_mock_response(json_body={"id": "111", "channel_id": "chan1"})
        client = _mock_client_with_responses(resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.send_message("chan1", "hello", "tok123")

        headers = client.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bot tok123"
        assert headers["Content-Type"] == "application/json"

    def test_correct_payload(self):
        resp = _make_mock_response(json_body={"id": "111", "channel_id": "chan1"})
        client = _mock_client_with_responses(resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.send_message("chan1", "hello", "tok123")

        payload = client.post.call_args[1]["json"]
        assert payload == {"content": "hello"}

    def test_returns_message_info(self):
        resp = _make_mock_response(json_body={"id": "msg-1", "channel_id": "chan1"})
        client = _mock_client_with_responses(resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            result = ds.send_message("chan1", "hello", "tok123")

        assert result == [{"message_id": "msg-1", "channel_id": "chan1"}]

    def test_chunked_message_sends_multiple_requests(self):
        resp1 = _make_mock_response(json_body={"id": "m1", "channel_id": "c1"})
        resp2 = _make_mock_response(json_body={"id": "m2", "channel_id": "c1"})
        client = _mock_client_with_responses(resp1, resp2)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            long_msg = "a" * 3000
            result = ds.send_message("c1", long_msg, "tok")

        assert client.post.call_count == 2
        assert len(result) == 2

    def test_http_error_exits(self):
        error_resp = _make_mock_response(status_code=403, json_body={"message": "Forbidden"})
        client = _mock_client_with_responses(error_resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            with pytest.raises(RuntimeError, match="403"):
                ds.send_message("chan1", "hello", "tok")


# ---------------------------------------------------------------------------
# reply_message() tests
# ---------------------------------------------------------------------------

class TestReplyMessage:
    """reply_message() sends correct HTTP requests for replies."""

    def test_first_chunk_has_message_reference(self):
        resp = _make_mock_response(json_body={"id": "r1", "channel_id": "c1"})
        client = _mock_client_with_responses(resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.reply_message("c1", "orig-msg", "short reply", "tok")

        payload = client.post.call_args[1]["json"]
        assert payload["message_reference"] == {"message_id": "orig-msg"}

    def test_subsequent_chunks_no_message_reference(self):
        resp1 = _make_mock_response(json_body={"id": "r1", "channel_id": "c1"})
        resp2 = _make_mock_response(json_body={"id": "r2", "channel_id": "c1"})
        client = _mock_client_with_responses(resp1, resp2)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.reply_message("c1", "orig-msg", "a" * 3000, "tok")

        # Second call should NOT have message_reference
        second_payload = client.post.call_args_list[1][1]["json"]
        assert "message_reference" not in second_payload

    def test_correct_url(self):
        resp = _make_mock_response(json_body={"id": "r1", "channel_id": "c1"})
        client = _mock_client_with_responses(resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.reply_message("c1", "orig-msg", "reply text", "tok")

        url = client.post.call_args[0][0]
        assert url == "https://discord.com/api/v10/channels/c1/messages"


# ---------------------------------------------------------------------------
# react_message() tests
# ---------------------------------------------------------------------------

class TestReactMessage:
    """react_message() sends correct HTTP requests for reactions."""

    def test_correct_url_with_encoded_emoji(self):
        resp = _make_mock_response(status_code=204, content=b"")
        client = _mock_client_with_responses(resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.react_message("c1", "m1", "\u2705", "tok")

        url = client.put.call_args[0][0]
        encoded = urllib.parse.quote("\u2705", safe="")
        expected_url = f"https://discord.com/api/v10/channels/c1/messages/m1/reactions/{encoded}/@me"
        assert url == expected_url
        assert "%E2%9C%85" in url

    def test_uses_put_method(self):
        resp = _make_mock_response(status_code=204, content=b"")
        client = _mock_client_with_responses(resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.react_message("c1", "m1", "\u2705", "tok")

        # put was called, post was not
        assert client.put.called
        assert not client.post.called

    def test_handles_204_no_content(self):
        resp = _make_mock_response(status_code=204, content=b"")
        client = _mock_client_with_responses(resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            # Should not raise
            ds.react_message("c1", "m1", "\u2705", "tok")


# ---------------------------------------------------------------------------
# load_token() tests
# ---------------------------------------------------------------------------

class TestLoadToken:
    """load_token() reads DISCORD_BOT_TOKEN from environment."""

    def test_returns_token_when_set(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token-123")
        with mock.patch("discord_send.load_dotenv"):
            token = ds.load_token()
        assert token == "test-token-123"

    def test_exits_when_token_missing(self, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        with mock.patch("discord_send.load_dotenv"):
            with pytest.raises(SystemExit) as exc_info:
                ds.load_token()
            assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# rename_thread() tests
# ---------------------------------------------------------------------------

class TestRenameThread:
    """rename_thread() sends correct HTTP requests."""

    def test_correct_url(self):
        resp = _make_mock_response(json_body={"id": "thread1", "name": "New Name"})
        client = _mock_client_with_responses(resp)
        client.patch = mock.Mock(return_value=resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.rename_thread("thread1", "New Name", "tok123")

        url = client.patch.call_args[0][0]
        assert url == "https://discord.com/api/v10/channels/thread1"

    def test_correct_payload(self):
        resp = _make_mock_response(json_body={"id": "thread1", "name": "New Name"})
        client = _mock_client_with_responses(resp)
        client.patch = mock.Mock(return_value=resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.rename_thread("thread1", "New Name", "tok123")

        payload = client.patch.call_args[1]["json"]
        assert payload == {"name": "New Name"}

    def test_name_truncated_to_100_chars(self):
        long_name = "a" * 150
        resp = _make_mock_response(json_body={"id": "thread1", "name": "a" * 100})
        client = _mock_client_with_responses(resp)
        client.patch = mock.Mock(return_value=resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.rename_thread("thread1", long_name, "tok123")

        payload = client.patch.call_args[1]["json"]
        assert payload == {"name": "a" * 100}

    def test_returns_channel_data(self):
        resp = _make_mock_response(json_body={"id": "thread1", "name": "New Name"})
        client = _mock_client_with_responses(resp)
        client.patch = mock.Mock(return_value=resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            result = ds.rename_thread("thread1", "New Name", "tok123")

        assert result == {"id": "thread1", "name": "New Name"}

    def test_http_error_raises(self):
        error_resp = _make_mock_response(status_code=403, json_body={"message": "Forbidden"})
        client = _mock_client_with_responses(error_resp)
        client.patch = mock.Mock(return_value=error_resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            with pytest.raises(RuntimeError, match="403"):
                ds.rename_thread("thread1", "New Name", "tok")

    def test_correct_auth_headers(self):
        resp = _make_mock_response(json_body={"id": "thread1", "name": "New Name"})
        client = _mock_client_with_responses(resp)
        client.patch = mock.Mock(return_value=resp)
        with mock.patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = mock.Mock(return_value=client)
            MockClient.return_value.__exit__ = mock.Mock(return_value=False)
            ds.rename_thread("thread1", "New Name", "tok123")

        headers = client.patch.call_args[1]["headers"]
        assert headers["Authorization"] == "Bot tok123"
        assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# Wrapper cwd test — invoke_claude passes cwd=merlin-bot/
# ---------------------------------------------------------------------------

class TestWrapperCwd:
    """invoke_claude() passes cwd to subprocess.run pointing to merlin-bot/."""

    def test_cwd_is_merlin_bot_dir(self, tmp_path, monkeypatch):
        import claude_wrapper as cw

        # Redirect logs to tmp
        log_dir = tmp_path / "logs" / "claude"
        monkeypatch.setattr(cw, "LOG_DIR", log_dir)

        with mock.patch("subprocess.run", return_value=mock.Mock(
            stdout="{}", stderr="", returncode=0
        )) as m:
            cw.invoke_claude("hello")

        assert m.call_args[1]["cwd"] == cw._SCRIPT_DIR
