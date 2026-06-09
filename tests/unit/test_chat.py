"""Tests for lib/chat.py — the merlin chat CLI over the Discord transport."""

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from lib import chat


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeTransport(SimpleNamespace):
    """Records transport calls and returns canned results."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def load_token(self):
        return "fake-token"

    def send_message(
        self, channel, content, token, *, files=None, thread_on_chunk=False
    ):
        self.calls.append(("send", channel, content, token, files, thread_on_chunk))
        return [{"message_id": "m1", "channel_id": channel}]

    def reply_message(self, channel, message, content, token, *, files=None):
        self.calls.append(("reply", channel, message, content, token, files))
        return [{"message_id": "m2", "channel_id": channel}]

    def react_message(self, channel, message, emoji, token):
        self.calls.append(("react", channel, message, emoji, token))

    def rename_thread(self, thread, name, token):
        self.calls.append(("rename", thread, name, token))
        return {"id": thread, "name": name}


@pytest.fixture
def transport(monkeypatch):
    fake = FakeTransport()
    monkeypatch.setattr(chat, "_transport", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Subcommand delegation
# ---------------------------------------------------------------------------


class TestChatCli:
    def test_send(self, transport, capsys):
        chat.main(["send", "--channel", "123", "--content", "hello"])
        assert transport.calls == [("send", "123", "hello", "fake-token", None, False)]
        out = json.loads(capsys.readouterr().out)
        assert out == {"message_id": "m1", "channel_id": "123"}

    def test_send_with_files_and_thread_on_chunk(self, transport, capsys):
        chat.main(
            [
                "send",
                "--channel",
                "123",
                "--content",
                "hi",
                "--file",
                "a.png",
                "--file",
                "b.png",
                "--thread-on-chunk",
            ]
        )
        kind, channel, content, token, files, thread_on_chunk = transport.calls[0]
        assert [f.name for f in files] == ["a.png", "b.png"]
        assert thread_on_chunk is True

    def test_reply(self, transport, capsys):
        chat.main(["reply", "--channel", "123", "--message", "456", "--content", "yo"])
        assert transport.calls[0][:4] == ("reply", "123", "456", "yo")
        out = json.loads(capsys.readouterr().out)
        assert out["message_id"] == "m2"

    def test_react(self, transport, capsys):
        chat.main(["react", "--channel", "123", "--message", "456", "--emoji", "✅"])
        assert transport.calls == [("react", "123", "456", "✅", "fake-token")]
        assert json.loads(capsys.readouterr().out) == {"ok": True}

    def test_rename_thread(self, transport, capsys):
        chat.main(["rename-thread", "--thread", "789", "--name", "Title"])
        assert transport.calls == [("rename", "789", "Title", "fake-token")]
        out = json.loads(capsys.readouterr().out)
        assert out == {"ok": True, "thread_id": "789", "name": "Title"}

    def test_no_subcommand_shows_help(self, transport, capsys):
        with pytest.raises(SystemExit) as exc_info:
            chat.main([])
        assert exc_info.value.code == 1
        assert "merlin chat" in capsys.readouterr().out

    def test_help_uses_merlin_chat_prog(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            chat.main(["--help"])
        assert exc_info.value.code == 0
        assert "merlin chat" in capsys.readouterr().out

    def test_leaf_missing_required_shows_help(self, transport, capsys):
        # `merlin chat reply` with no args prints the leaf's full help (all
        # options) plus the error, not just a one-line usage.
        with pytest.raises(SystemExit) as exc_info:
            chat.main(["reply"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "usage: merlin chat reply" in err
        assert "--channel" in err and "--message" in err
        assert "the following arguments are required" in err


# ---------------------------------------------------------------------------
# HTTP layer (real transport, httpx mocked)
# ---------------------------------------------------------------------------


def _response(body: dict, status: int = 200):
    resp = mock.Mock()
    resp.status_code = status
    resp.content = json.dumps(body).encode()
    resp.json.return_value = body
    return resp


class TestChatHttpLayer:
    def test_send_hits_discord_api(self, monkeypatch, capsys):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-123")

        body = {"id": "9001", "channel_id": "123"}
        with mock.patch("httpx.Client") as MockClient:
            client = MockClient.return_value.__enter__.return_value
            client.post.return_value = _response(body)

            chat.main(["send", "--channel", "123", "--content", "hello"])

            url = client.post.call_args[0][0]
            assert url.endswith("/channels/123/messages")
            headers = client.post.call_args.kwargs["headers"]
            assert headers["Authorization"] == "Bot tok-123"

        out = json.loads(capsys.readouterr().out)
        assert out == {"message_id": "9001", "channel_id": "123"}


# ---------------------------------------------------------------------------
# CLI routing
# ---------------------------------------------------------------------------


class TestChatRouting:
    def test_cli_main_routes_chat(self, monkeypatch, capsys):
        from cli import cli_main

        with pytest.raises(SystemExit) as exc_info:
            cli_main(["chat", "--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "merlin chat" in out
        assert "rename-thread" in out
