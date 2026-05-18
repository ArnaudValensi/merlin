"""Tests for terminal module — WebSocket PTY bridge with tmux persistence."""

import asyncio
import json
import os
import struct
import tempfile
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

        with (
            mock.patch("pty.fork", return_value=(999, 5)),
            mock.patch("os.read", return_value=b""),
            mock.patch("os.close"),
            mock.patch("os.kill"),
            mock.patch("os.waitpid"),
        ):
            from starlette.websockets import WebSocketDisconnect

            ws.receive_text.side_effect = WebSocketDisconnect()
            asyncio.run(tr.terminal_ws(ws))

        ws.accept.assert_called_once()

    def test_cleanup_on_disconnect(self, mock_websocket_with_cookie):
        """PTY fd is closed and child process killed on disconnect."""
        ws = mock_websocket_with_cookie

        child_pid = 12345
        master_fd = 7

        with (
            mock.patch("pty.fork", return_value=(child_pid, master_fd)),
            mock.patch("os.read", return_value=b""),
            mock.patch("os.close") as mock_close,
            mock.patch("os.kill") as mock_kill,
            mock.patch("os.waitpid") as mock_waitpid,
        ):
            from starlette.websockets import WebSocketDisconnect

            ws.receive_text.side_effect = WebSocketDisconnect()
            asyncio.run(tr.terminal_ws(ws))

        # Verify cleanup
        mock_close.assert_called_with(master_fd)
        mock_kill.assert_called_with(child_pid, mock.ANY)
        mock_waitpid.assert_called_with(child_pid, mock.ANY)


# ---------------------------------------------------------------------------
# PTY Registry
# ---------------------------------------------------------------------------


class TestPtyRegistry:
    """PTY registry for server-side text injection."""

    def setup_method(self):
        """Clear registry before each test."""
        tr._pty_registry.clear()

    def test_register_pty_stores_fd(self):
        tr.register_pty("terminal", 42)
        assert tr.get_pty_fd("terminal") == 42

    def test_get_pty_fd_returns_none_when_empty(self):
        assert tr.get_pty_fd("terminal") is None

    def test_unregister_pty_removes_entry(self):
        tr.register_pty("terminal", 42)
        tr.unregister_pty("terminal")
        assert tr.get_pty_fd("terminal") is None

    def test_unregister_pty_noop_when_missing(self):
        tr.unregister_pty("nonexistent")  # should not raise

    def test_register_pty_overwrites(self):
        tr.register_pty("terminal", 42)
        tr.register_pty("terminal", 99)
        assert tr.get_pty_fd("terminal") == 99


# ---------------------------------------------------------------------------
# Transcription API
# ---------------------------------------------------------------------------


class TestTranscribeEndpoint:
    """POST /api/transcribe endpoint."""

    def setup_method(self):
        """Clear PTY registry so tests get fallback (200) path by default."""
        tr._pty_registry.clear()

    def _make_file(self, data=b"fake audio data", filename="recording.webm"):
        f = mock.AsyncMock()
        f.filename = filename
        f.read = mock.AsyncMock(return_value=data)
        return f

    def test_transcribe_returns_text(self):
        """Successful transcription returns JSON with text (fallback path)."""
        with (
            mock.patch("transcribe.transcribe", return_value="hello world"),
            mock.patch("os.unlink"),
        ):
            result = asyncio.run(
                tr.transcribe_audio(
                    file=self._make_file(),
                    language="en",
                    auto_enter="false",
                    _auth=None,
                )
            )

        assert result.status_code == 200
        body = json.loads(result.body)
        assert body["text"] == "hello world"

    def test_transcribe_cleans_up_temp_file(self):
        """Temp file is deleted after transcription (fallback path)."""
        with (
            mock.patch("transcribe.transcribe", return_value="text"),
            mock.patch("os.unlink") as mock_unlink,
        ):
            asyncio.run(
                tr.transcribe_audio(
                    file=self._make_file(),
                    language="en",
                    auto_enter="false",
                    _auth=None,
                )
            )

        mock_unlink.assert_called_once()

    def test_transcribe_error_returns_500(self):
        """Transcription failure returns 500 with error message."""
        with (
            mock.patch(
                "transcribe.transcribe", side_effect=RuntimeError("model failed")
            ),
            mock.patch("os.unlink"),
        ):
            result = asyncio.run(
                tr.transcribe_audio(
                    file=self._make_file(),
                    language="en",
                    auto_enter="false",
                    _auth=None,
                )
            )

        assert result.status_code == 500
        body = json.loads(result.body)
        assert "error" in body

    def test_transcribe_passes_language(self):
        """Language parameter is forwarded to transcribe()."""
        with (
            mock.patch(
                "transcribe.transcribe", return_value="bonjour"
            ) as mock_transcribe,
            mock.patch("os.unlink"),
        ):
            asyncio.run(
                tr.transcribe_audio(
                    file=self._make_file(),
                    language="fr",
                    auto_enter="false",
                    _auth=None,
                )
            )

        assert mock_transcribe.call_args[0][1] == "fr"

    def test_transcribe_defaults_to_english(self):
        """Default language is English when explicitly passed."""
        with (
            mock.patch(
                "transcribe.transcribe", return_value="hello"
            ) as mock_transcribe,
            mock.patch("os.unlink"),
        ):
            asyncio.run(
                tr.transcribe_audio(
                    file=self._make_file(),
                    language="en",
                    auto_enter="false",
                    _auth=None,
                )
            )

        assert mock_transcribe.call_args[0][1] == "en"

    def test_transcribe_rejects_invalid_language(self):
        """Invalid language falls back to English."""
        with (
            mock.patch(
                "transcribe.transcribe", return_value="hello"
            ) as mock_transcribe,
            mock.patch("os.unlink"),
        ):
            asyncio.run(
                tr.transcribe_audio(
                    file=self._make_file(),
                    language="xx",
                    auto_enter="false",
                    _auth=None,
                )
            )

        assert mock_transcribe.call_args[0][1] == "en"


class TestTranscribeSizeLimit:
    """POST /api/transcribe rejects audio exceeding 100 MB."""

    def setup_method(self):
        tr._pty_registry.clear()

    def _make_file(self, data=b"fake audio data", filename="recording.webm"):
        f = mock.AsyncMock()
        f.filename = filename
        f.read = mock.AsyncMock(return_value=data)
        return f

    def test_rejects_oversized_audio(self):
        """Audio >25 MB returns 413."""
        big_data = b"x" * (25 * 1024 * 1024 + 1)
        result = asyncio.run(
            tr.transcribe_audio(
                file=self._make_file(data=big_data),
                language="en",
                auto_enter="false",
                _auth=None,
            )
        )
        assert result.status_code == 413
        body = json.loads(result.body)
        assert "too large" in body["error"].lower()

    def test_accepts_exact_limit(self):
        """Audio exactly 25 MB is accepted."""
        data = b"x" * (25 * 1024 * 1024)
        with (
            mock.patch("transcribe.transcribe", return_value="ok"),
            mock.patch("os.unlink"),
        ):
            result = asyncio.run(
                tr.transcribe_audio(
                    file=self._make_file(data=data),
                    language="en",
                    auto_enter="false",
                    _auth=None,
                )
            )
        assert result.status_code == 200


class TestTranscribeServerSideInjection:
    """POST /api/transcribe with PTY registered (202 path)."""

    def setup_method(self):
        tr._pty_registry.clear()

    def _make_file(self, data=b"fake audio", filename="recording.webm"):
        f = mock.AsyncMock()
        f.filename = filename
        f.read = mock.AsyncMock(return_value=data)
        return f

    def test_returns_202_when_pty_registered(self):
        """With PTY registered, returns 202 and schedules background task."""
        r_fd, w_fd = os.pipe()
        tr.register_pty("terminal", w_fd)
        try:
            with mock.patch("transcribe.transcribe", return_value="hello from server"):
                result = asyncio.run(
                    self._run_with_tasks(
                        self._make_file(),
                        "en",
                        "false",
                    )
                )

            assert result.status_code == 202
            body = json.loads(result.body)
            assert body["status"] == "accepted"

            # Read what was written to the PTY
            os.close(w_fd)
            w_fd = -1
            data = os.read(r_fd, 4096)
            assert data == b"hello from server"
        finally:
            tr._pty_registry.clear()
            if w_fd != -1:
                os.close(w_fd)
            os.close(r_fd)

    def test_auto_enter_appends_newline(self):
        """auto_enter=true writes text + newline to PTY."""
        r_fd, w_fd = os.pipe()
        tr.register_pty("terminal", w_fd)
        try:
            with mock.patch("transcribe.transcribe", return_value="git status"):
                result = asyncio.run(
                    self._run_with_tasks(
                        self._make_file(),
                        "en",
                        "true",
                    )
                )

            assert result.status_code == 202
            os.close(w_fd)
            w_fd = -1
            data = os.read(r_fd, 4096)
            assert data == b"git status\r"
        finally:
            tr._pty_registry.clear()
            if w_fd != -1:
                os.close(w_fd)
            os.close(r_fd)

    def test_no_enter_by_default(self):
        """auto_enter=false writes text without newline."""
        r_fd, w_fd = os.pipe()
        tr.register_pty("terminal", w_fd)
        try:
            with mock.patch("transcribe.transcribe", return_value="hello"):
                result = asyncio.run(
                    self._run_with_tasks(
                        self._make_file(),
                        "en",
                        "false",
                    )
                )

            assert result.status_code == 202
            os.close(w_fd)
            w_fd = -1
            data = os.read(r_fd, 4096)
            assert data == b"hello"
        finally:
            tr._pty_registry.clear()
            if w_fd != -1:
                os.close(w_fd)
            os.close(r_fd)

    def test_closed_fd_no_crash(self):
        """Closed PTY fd logs a warning but doesn't crash."""
        r_fd, w_fd = os.pipe()
        os.close(w_fd)
        os.close(r_fd)
        tr.register_pty("terminal", w_fd)
        try:
            with mock.patch("transcribe.transcribe", return_value="hello"):
                # Should not raise
                result = asyncio.run(
                    self._run_with_tasks(
                        self._make_file(),
                        "en",
                        "false",
                    )
                )
            assert result.status_code == 202
        finally:
            tr._pty_registry.clear()

    def test_temp_file_cleanup(self):
        """Temp file is cleaned up after background transcription."""
        r_fd, w_fd = os.pipe()
        tr.register_pty("terminal", w_fd)
        created_files = []
        orig_named_temp = tempfile.NamedTemporaryFile

        def tracking_temp(**kwargs):
            tmp = orig_named_temp(**kwargs)
            created_files.append(tmp.name)
            return tmp

        try:
            with (
                mock.patch("transcribe.transcribe", return_value="text"),
                mock.patch("tempfile.NamedTemporaryFile", side_effect=tracking_temp),
            ):
                asyncio.run(
                    self._run_with_tasks(
                        self._make_file(),
                        "en",
                        "false",
                    )
                )

            os.close(w_fd)
            w_fd = -1
            os.close(r_fd)
            r_fd = -1

            # Temp file should have been cleaned up
            assert len(created_files) == 1
            assert not os.path.exists(created_files[0])
        finally:
            tr._pty_registry.clear()
            if w_fd != -1:
                os.close(w_fd)
            if r_fd != -1:
                os.close(r_fd)

    def test_transcription_exception_cleanup(self):
        """Temp file is cleaned up even when transcription raises."""
        r_fd, w_fd = os.pipe()
        tr.register_pty("terminal", w_fd)
        created_files = []
        orig_named_temp = tempfile.NamedTemporaryFile

        def tracking_temp(**kwargs):
            tmp = orig_named_temp(**kwargs)
            created_files.append(tmp.name)
            return tmp

        try:
            with (
                mock.patch("transcribe.transcribe", side_effect=RuntimeError("fail")),
                mock.patch("tempfile.NamedTemporaryFile", side_effect=tracking_temp),
            ):
                asyncio.run(
                    self._run_with_tasks(
                        self._make_file(),
                        "en",
                        "false",
                    )
                )

            os.close(w_fd)
            w_fd = -1
            os.close(r_fd)
            r_fd = -1

            assert len(created_files) == 1
            assert not os.path.exists(created_files[0])
        finally:
            tr._pty_registry.clear()
            if w_fd != -1:
                os.close(w_fd)
            if r_fd != -1:
                os.close(r_fd)

    def test_200_fallback_when_no_pty(self):
        """Without PTY registered, returns 200 with text (fallback)."""
        with (
            mock.patch("transcribe.transcribe", return_value="fallback text"),
            mock.patch("os.unlink"),
        ):
            result = asyncio.run(
                tr.transcribe_audio(
                    file=self._make_file(),
                    language="en",
                    auto_enter="false",
                    _auth=None,
                )
            )

        assert result.status_code == 200
        body = json.loads(result.body)
        assert body["text"] == "fallback text"

    def test_concurrent_requests_no_corruption(self):
        """Two concurrent transcriptions don't corrupt each other."""
        r_fd, w_fd = os.pipe()
        tr.register_pty("terminal", w_fd)

        call_count = 0

        def mock_transcribe(path, lang):
            nonlocal call_count
            call_count += 1
            return f"result-{call_count}"

        try:
            with mock.patch("transcribe.transcribe", side_effect=mock_transcribe):

                async def run():
                    r1 = await tr.transcribe_audio(
                        file=self._make_file(b"audio1"),
                        language="en",
                        auto_enter="false",
                        _auth=None,
                    )
                    r2 = await tr.transcribe_audio(
                        file=self._make_file(b"audio2"),
                        language="fr",
                        auto_enter="false",
                        _auth=None,
                    )
                    # Drain background tasks
                    tasks = [
                        t
                        for t in asyncio.all_tasks()
                        if t is not asyncio.current_task()
                    ]
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    return r1, r2

                r1, r2 = asyncio.run(run())

            assert r1.status_code == 202
            assert r2.status_code == 202

            # Both results should have been written to the pipe
            os.close(w_fd)
            w_fd = -1
            data = os.read(r_fd, 4096)
            assert b"result-1" in data
            assert b"result-2" in data
        finally:
            tr._pty_registry.clear()
            if w_fd != -1:
                os.close(w_fd)
            os.close(r_fd)

    def test_empty_transcription_no_write(self):
        """Empty transcription result doesn't write to PTY."""
        r_fd, w_fd = os.pipe()
        tr.register_pty("terminal", w_fd)
        try:
            with mock.patch("transcribe.transcribe", return_value=""):
                result = asyncio.run(
                    self._run_with_tasks(
                        self._make_file(),
                        "en",
                        "true",
                    )
                )

            assert result.status_code == 202
            os.close(w_fd)
            w_fd = -1
            data = os.read(r_fd, 4096)
            assert data == b""  # Nothing written, not even \r
        finally:
            tr._pty_registry.clear()
            if w_fd != -1:
                os.close(w_fd)
            os.close(r_fd)

    async def _run_with_tasks(self, file, language, auto_enter):
        """Run transcribe_audio and then drain pending tasks."""
        result = await tr.transcribe_audio(
            file=file,
            language=language,
            auto_enter=auto_enter,
            _auth=None,
        )
        # Let the background task complete
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return result


# ---------------------------------------------------------------------------
# Terminal CWD API
# ---------------------------------------------------------------------------


class TestTerminalCwd:
    """GET /api/terminal/cwd endpoint."""

    def test_returns_cwd_in_git_repo(self):
        """Returns CWD with is_git_repo=True when in a git repo."""

        async def run():
            with mock.patch("asyncio.create_subprocess_exec") as mock_exec:
                # First call: tmux display-message
                tmux_proc = mock.AsyncMock()
                tmux_proc.communicate.return_value = (b"/home/user/project\n", b"")
                tmux_proc.returncode = 0
                # Second call: git rev-parse
                git_proc = mock.AsyncMock()
                git_proc.communicate.return_value = (b"/home/user/project\n", b"")
                git_proc.returncode = 0
                mock_exec.side_effect = [tmux_proc, git_proc]

                result = await tr.api_terminal_cwd(_auth=None)

            body = json.loads(result.body)
            assert body["cwd"] == "/home/user/project"
            assert body["is_git_repo"] is True
            assert body["repo_root"] == "/home/user/project"

        asyncio.run(run())

    def test_returns_null_when_tmux_fails(self):
        """Returns null CWD when tmux is not running."""

        async def run():
            with mock.patch("asyncio.create_subprocess_exec") as mock_exec:
                proc = mock.AsyncMock()
                proc.communicate.return_value = (b"", b"no server running")
                proc.returncode = 1
                mock_exec.return_value = proc

                result = await tr.api_terminal_cwd(_auth=None)

            body = json.loads(result.body)
            assert body["cwd"] is None
            assert body["is_git_repo"] is False

        asyncio.run(run())

    def test_returns_not_git_repo(self):
        """Returns is_git_repo=False when CWD is not in a git repo."""

        async def run():
            with mock.patch("asyncio.create_subprocess_exec") as mock_exec:
                tmux_proc = mock.AsyncMock()
                tmux_proc.communicate.return_value = (b"/tmp\n", b"")
                tmux_proc.returncode = 0
                git_proc = mock.AsyncMock()
                git_proc.communicate.return_value = (b"", b"fatal: not a git repo")
                git_proc.returncode = 128
                mock_exec.side_effect = [tmux_proc, git_proc]

                result = await tr.api_terminal_cwd(_auth=None)

            body = json.loads(result.body)
            assert body["cwd"] == "/tmp"
            assert body["is_git_repo"] is False
            assert body["repo_root"] is None

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Clipboard sync
# ---------------------------------------------------------------------------


class TestClipboardSync:
    """_sync_clipboard writes text to /tmp/merlin-clipboard/current.txt."""

    @pytest.fixture(autouse=True)
    def _setup_clipboard_dir(self, tmp_path, monkeypatch):
        """Redirect CLIPBOARD_DIR to tmp for isolation."""
        monkeypatch.setattr(tr, "CLIPBOARD_DIR", tmp_path / "clipboard")
        self.clip_dir = tmp_path / "clipboard"

    def test_writes_text_to_file(self):
        tr._sync_clipboard("hello world")
        assert (self.clip_dir / "current.txt").read_text() == "hello world"

    def test_creates_directory(self):
        assert not self.clip_dir.exists()
        tr._sync_clipboard("test")
        assert self.clip_dir.exists()

    def test_overwrites_previous(self):
        tr._sync_clipboard("first")
        tr._sync_clipboard("second")
        assert (self.clip_dir / "current.txt").read_text() == "second"

    def test_empty_text(self):
        tr._sync_clipboard("")
        assert (self.clip_dir / "current.txt").read_text() == ""

    def test_unicode_text(self):
        text = "Hello \u4e16\u754c \U0001f680 caf\u00e9"
        tr._sync_clipboard(text)
        assert (self.clip_dir / "current.txt").read_text() == text

    def test_large_text(self):
        text = "x" * (1024 * 1024)  # 1MB
        tr._sync_clipboard(text)
        assert (self.clip_dir / "current.txt").read_text() == text

    def test_atomic_write_no_partial(self):
        """Temp file should not linger after successful write."""
        tr._sync_clipboard("data")
        assert not (self.clip_dir / ".current.tmp").exists()
        assert (self.clip_dir / "current.txt").exists()


class TestSafeBasename:
    """_safe_basename sanitizes filenames for shell-safe path injection."""

    def test_keeps_simple_name(self):
        assert tr._safe_basename("report.pdf") == "report.pdf"

    def test_strips_path_components(self):
        assert tr._safe_basename("/etc/passwd") == "passwd"
        assert tr._safe_basename("../../etc/passwd") == "passwd"

    def test_replaces_whitespace(self):
        assert tr._safe_basename("my file.txt") == "my_file.txt"

    def test_replaces_shell_metacharacters(self):
        assert tr._safe_basename("a;b&c|d.txt") == "a_b_c_d.txt"
        assert tr._safe_basename("$(rm).sh") == "__rm_.sh"

    def test_strips_leading_dots(self):
        assert tr._safe_basename(".bashrc") == "bashrc"

    def test_caps_length(self):
        long = "a" * 200 + ".txt"
        out = tr._safe_basename(long)
        assert len(out) <= 80
        assert out.endswith(".txt")

    def test_empty_falls_back_to_file(self):
        assert tr._safe_basename("") == "file"
        assert tr._safe_basename(None) == "file"
        assert tr._safe_basename("///") == "file"
