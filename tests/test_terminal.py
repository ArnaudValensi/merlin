# /// script
# dependencies = ["pytest"]
# ///
"""Tests for terminal module — WebSocket PTY bridge with tmux persistence."""

import asyncio
import json
import struct
from unittest import mock

import pytest

from terminal import routes as tr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_auth(monkeypatch):
    """Mock auth module for consistent test credentials."""
    import auth
    monkeypatch.setattr(auth, "_get_password", lambda: "secret123")


# ---------------------------------------------------------------------------
# PTY helpers
# ---------------------------------------------------------------------------

class TestSetWinsize:
    """_set_winsize calls ioctl with correct struct."""

    def test_calls_ioctl_with_packed_size(self):
        with mock.patch("terminal.routes.fcntl.ioctl") as mock_ioctl:
            tr._set_winsize(5, 80, 24)
            mock_ioctl.assert_called_once()
            args = mock_ioctl.call_args
            assert args[0][0] == 5  # fd
            # Verify the packed struct: rows=24, cols=80
            expected = struct.pack("HHHH", 24, 80, 0, 0)
            assert args[0][2] == expected

    def test_different_dimensions(self):
        with mock.patch("terminal.routes.fcntl.ioctl") as mock_ioctl:
            tr._set_winsize(10, 120, 40)
            packed = mock_ioctl.call_args[0][2]
            rows, cols, _, _ = struct.unpack("HHHH", packed)
            assert rows == 40
            assert cols == 120


class TestReadPty:
    """_read_pty reads from fd and decodes."""

    def test_reads_and_decodes(self):
        with mock.patch("os.read", return_value=b"hello world"):
            result = tr._read_pty(5)
        assert result == "hello world"

    def test_returns_none_on_empty(self):
        with mock.patch("os.read", return_value=b""):
            result = tr._read_pty(5)
        assert result is None

    def test_returns_none_on_oserror(self):
        with mock.patch("os.read", side_effect=OSError("fd closed")):
            result = tr._read_pty(5)
        assert result is None

    def test_replaces_invalid_utf8(self):
        with mock.patch("os.read", return_value=b"hello \xff world"):
            result = tr._read_pty(5)
        assert "hello" in result
        assert "world" in result


# ---------------------------------------------------------------------------
# WebSocket lifecycle (mocked PTY)
# ---------------------------------------------------------------------------

class TestTerminalWebSocket:
    """WebSocket endpoint behavior with mocked PTY."""

    @pytest.fixture
    def mock_websocket_with_cookie(self):
        """Create a mock WebSocket with a valid session cookie."""
        import auth
        cookie_val = auth.sign_cookie("admin", 9999999999, "secret123")
        ws = mock.AsyncMock()
        ws.headers = {}
        ws.query_params = {}
        ws.cookies = {"session": cookie_val}
        return ws

    @pytest.fixture
    def mock_websocket_no_auth(self):
        """Create a mock WebSocket without auth."""
        ws = mock.AsyncMock()
        ws.headers = {}
        ws.query_params = {}
        ws.cookies = {}
        return ws

    def test_rejects_unauthorized(self, mock_websocket_no_auth):
        """WebSocket without auth gets closed with 4401."""
        ws = mock_websocket_no_auth
        asyncio.run(tr.terminal_ws(ws))
        ws.close.assert_called_once_with(code=4401, reason="Unauthorized")
        ws.accept.assert_not_called()

    def test_accepts_cookie_auth(self, mock_websocket_with_cookie):
        """WebSocket auth via session cookie."""
        ws = mock_websocket_with_cookie

        with mock.patch("pty.fork", return_value=(999, 5)), \
             mock.patch("os.read", return_value=b""), \
             mock.patch("os.close"), \
             mock.patch("os.kill"), \
             mock.patch("os.waitpid"):
            from starlette.websockets import WebSocketDisconnect
            ws.receive_text.side_effect = WebSocketDisconnect()
            asyncio.run(tr.terminal_ws(ws))

        ws.accept.assert_called_once()

    def test_cleanup_on_disconnect(self, mock_websocket_with_cookie):
        """PTY fd is closed and child process killed on disconnect."""
        ws = mock_websocket_with_cookie

        child_pid = 12345
        master_fd = 7

        with mock.patch("pty.fork", return_value=(child_pid, master_fd)), \
             mock.patch("os.read", return_value=b""), \
             mock.patch("os.close") as mock_close, \
             mock.patch("os.kill") as mock_kill, \
             mock.patch("os.waitpid") as mock_waitpid:
            from starlette.websockets import WebSocketDisconnect
            ws.receive_text.side_effect = WebSocketDisconnect()
            asyncio.run(tr.terminal_ws(ws))

        # Verify cleanup
        mock_close.assert_called_with(master_fd)
        mock_kill.assert_called_with(child_pid, mock.ANY)
        mock_waitpid.assert_called_with(child_pid, mock.ANY)


# ---------------------------------------------------------------------------
# Transcription API
# ---------------------------------------------------------------------------

class TestTranscribeEndpoint:
    """POST /api/transcribe endpoint."""

    def test_transcribe_returns_text(self):
        """Successful transcription returns JSON with text."""
        mock_file = mock.AsyncMock()
        mock_file.filename = "recording.webm"
        mock_file.read = mock.AsyncMock(return_value=b"fake audio data")

        with mock.patch("transcribe.transcribe", return_value="hello world"), \
             mock.patch("os.unlink"):
            result = asyncio.run(tr.transcribe_audio(file=mock_file, language="en", _auth=None))

        assert result.status_code == 200
        body = json.loads(result.body)
        assert body["text"] == "hello world"

    def test_transcribe_cleans_up_temp_file(self):
        """Temp file is deleted after transcription."""
        mock_file = mock.AsyncMock()
        mock_file.filename = "audio.webm"
        mock_file.read = mock.AsyncMock(return_value=b"data")

        with mock.patch("transcribe.transcribe", return_value="text"), \
             mock.patch("os.unlink") as mock_unlink:
            asyncio.run(tr.transcribe_audio(file=mock_file, language="en", _auth=None))

        mock_unlink.assert_called_once()

    def test_transcribe_error_returns_500(self):
        """Transcription failure returns 500 with error message."""
        mock_file = mock.AsyncMock()
        mock_file.filename = "audio.webm"
        mock_file.read = mock.AsyncMock(return_value=b"data")

        with mock.patch("transcribe.transcribe", side_effect=RuntimeError("model failed")), \
             mock.patch("os.unlink"):
            result = asyncio.run(tr.transcribe_audio(file=mock_file, language="en", _auth=None))

        assert result.status_code == 500
        body = json.loads(result.body)
        assert "error" in body

    def test_transcribe_passes_language(self):
        """Language parameter is forwarded to transcribe()."""
        mock_file = mock.AsyncMock()
        mock_file.filename = "audio.webm"
        mock_file.read = mock.AsyncMock(return_value=b"data")

        with mock.patch("transcribe.transcribe", return_value="bonjour") as mock_transcribe, \
             mock.patch("os.unlink"):
            asyncio.run(tr.transcribe_audio(file=mock_file, language="fr", _auth=None))

        # Second arg to transcribe should be the language
        assert mock_transcribe.call_args[0][1] == "fr"

    def test_transcribe_defaults_to_english(self):
        """Default language is English when explicitly passed."""
        mock_file = mock.AsyncMock()
        mock_file.filename = "audio.webm"
        mock_file.read = mock.AsyncMock(return_value=b"data")

        with mock.patch("transcribe.transcribe", return_value="hello") as mock_transcribe, \
             mock.patch("os.unlink"):
            asyncio.run(tr.transcribe_audio(file=mock_file, language="en", _auth=None))

        assert mock_transcribe.call_args[0][1] == "en"

    def test_transcribe_rejects_invalid_language(self):
        """Invalid language falls back to English."""
        mock_file = mock.AsyncMock()
        mock_file.filename = "audio.webm"
        mock_file.read = mock.AsyncMock(return_value=b"data")

        with mock.patch("transcribe.transcribe", return_value="hello") as mock_transcribe, \
             mock.patch("os.unlink"):
            asyncio.run(tr.transcribe_audio(file=mock_file, language="xx", _auth=None))

        assert mock_transcribe.call_args[0][1] == "en"
