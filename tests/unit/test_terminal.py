"""Tests for terminal module — WebSocket PTY bridge with tmux persistence."""

import asyncio
import json
import os
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


# ---------------------------------------------------------------------------
# WebSocket lifecycle (real PTY, mocked fork/child)
# ---------------------------------------------------------------------------


class TestTerminalWebSocket:
    """WebSocket endpoint behavior with mocked PTY."""

    @pytest.fixture(autouse=True)
    def known_empty_session_sweep(self, monkeypatch):
        """WebSocket unit tests never depend on the developer's tmux server."""
        monkeypatch.setattr(tr.board_sweep, "run_session_sweep_checked", lambda: [])

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
        import pty as _pty

        ws = mock_websocket_with_cookie
        master, slave = _pty.openpty()

        try:
            with (
                mock.patch("pty.fork", return_value=(999, master)),
                mock.patch("os.waitpid", return_value=(0, 0)),
                mock.patch(
                    "terminal.routes.terminate_client", new_callable=mock.AsyncMock
                ),
            ):
                from starlette.websockets import WebSocketDisconnect

                ws.receive_text.side_effect = WebSocketDisconnect()
                asyncio.run(tr.terminal_ws(ws))

            ws.accept.assert_called_once()
        finally:
            os.close(slave)

    def test_passes_requested_session_identity_to_reconnect_policy(
        self, mock_websocket_with_cookie
    ):
        """The initial attach decision receives the tab's WebSocket preference."""
        import pty as _pty

        from terminal.tmux import SessionIdentity

        ws = mock_websocket_with_cookie
        ws.query_params = {
            "session_id": "$4",
            "session_created": "1699999900",
        }
        master, slave = _pty.openpty()
        sessions = [mock.sentinel.session]

        try:
            with (
                mock.patch("pty.fork", return_value=(999, master)),
                mock.patch("os.waitpid", return_value=(0, 0)),
                mock.patch(
                    "terminal.routes.board_sweep.run_session_sweep_checked",
                    return_value=sessions,
                ),
                mock.patch(
                    "terminal.routes.reconnect_argv",
                    return_value=["tmux", "attach"],
                ) as choose,
                mock.patch(
                    "terminal.routes.terminate_client", new_callable=mock.AsyncMock
                ),
            ):
                from starlette.websockets import WebSocketDisconnect

                ws.receive_text.side_effect = WebSocketDisconnect()
                asyncio.run(tr.terminal_ws(ws))

            choose.assert_called_once_with(sessions, SessionIdentity("$4", 1699999900))
        finally:
            os.close(slave)

    def test_transient_tmux_sweep_failure_retries_without_forking(
        self, mock_websocket_with_cookie
    ):
        """An unknown sweep must not create merlin-dev or consume the tab pin."""
        ws = mock_websocket_with_cookie
        ws.query_params = {
            "session_id": "$4",
            "session_created": "1699999900",
        }

        with (
            mock.patch(
                "terminal.routes.board_sweep.run_session_sweep_checked",
                return_value=None,
            ),
            mock.patch("terminal.routes.reconnect_argv") as choose,
            mock.patch("pty.fork") as fork,
        ):
            asyncio.run(tr.terminal_ws(ws))

        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(
            code=1013, reason="tmux temporarily unavailable"
        )
        choose.assert_not_called()
        fork.assert_not_called()

    def test_unresolvable_client_tty_fails_the_connection(
        self, mock_websocket_with_cookie
    ):
        """Never serve a terminal that runs but cannot switch sessions.

        A tty we cannot derive means per-client switching is impossible. That
        must tear the connection down rather than degrade silently — the
        silent-degrade path is exactly how the macOS /proc breakage hid. This
        pins the orchestration: no bridge, the master fd closed, the child
        handed to terminate_client, and an error close code.
        """
        import pty as _pty

        ws = mock_websocket_with_cookie
        master, slave = _pty.openpty()

        try:
            with (
                mock.patch("pty.fork", return_value=(999, master)),
                mock.patch("os.ptsname", side_effect=OSError("no pty")),
                mock.patch(
                    "terminal.routes.terminate_client", new_callable=mock.AsyncMock
                ) as mock_terminate,
                mock.patch("terminal.routes.PtyBridge") as bridge_cls,
            ):
                asyncio.run(tr.terminal_ws(ws))

            bridge_cls.assert_not_called()
            mock_terminate.assert_awaited_once_with(999)
            ws.close.assert_awaited_once_with(code=1011, reason="PTY setup failed")

            # The master fd is really released, not merely "cleanup was called":
            # fstat on a closed descriptor raises. terminate_client is mocked
            # here, so reaping itself is covered by test_cleanup_on_disconnect.
            with pytest.raises(OSError):
                os.fstat(master)
        finally:
            os.close(slave)

    def test_cleanup_on_disconnect(self, mock_websocket_with_cookie):
        """On disconnect the PTY bridge is closed and the tmux client terminated."""
        import pty as _pty

        ws = mock_websocket_with_cookie
        child_pid = 12345
        master, slave = _pty.openpty()

        captured = {}
        orig_bridge_cls = tr.PtyBridge

        def capture_bridge(fd):
            bridge = orig_bridge_cls(fd)
            captured["bridge"] = bridge
            return bridge

        try:
            with (
                mock.patch("pty.fork", return_value=(child_pid, master)),
                mock.patch("os.waitpid", return_value=(0, 0)),
                mock.patch("terminal.routes.PtyBridge", side_effect=capture_bridge),
                mock.patch(
                    "terminal.routes.terminate_client", new_callable=mock.AsyncMock
                ) as mock_terminate,
            ):
                from starlette.websockets import WebSocketDisconnect

                ws.receive_text.side_effect = WebSocketDisconnect()
                asyncio.run(tr.terminal_ws(ws))

            # Verify cleanup: bridge closed, client terminated
            assert captured["bridge"].closed is True
            mock_terminate.assert_awaited_once_with(child_pid)
        finally:
            os.close(slave)

    def test_pty_output_reaches_websocket(self, mock_websocket_with_cookie):
        """Bytes written on the PTY slave side arrive as websocket text."""
        import pty as _pty
        import tty as _tty

        ws = mock_websocket_with_cookie
        master, slave = _pty.openpty()
        _tty.setraw(slave)

        sent: list[str] = []
        got_output = asyncio.Event()

        async def fake_send_text(text):
            sent.append(text)
            got_output.set()

        async def fake_receive_text():
            # Hold the connection open until output arrived, then disconnect
            from starlette.websockets import WebSocketDisconnect

            await asyncio.wait_for(got_output.wait(), timeout=5)
            raise WebSocketDisconnect()

        ws.send_text = fake_send_text
        ws.receive_text = fake_receive_text

        async def run():
            with (
                mock.patch("pty.fork", return_value=(999, master)),
                mock.patch("os.waitpid", return_value=(0, 0)),
                mock.patch(
                    "terminal.routes.terminate_client", new_callable=mock.AsyncMock
                ),
            ):
                handler = asyncio.create_task(tr.terminal_ws(ws))
                await asyncio.sleep(0.05)
                os.write(slave, b"hello from pty")
                await asyncio.wait_for(handler, timeout=5)

        try:
            asyncio.run(run())
            assert "hello from pty" in "".join(sent)
        finally:
            os.close(slave)


# ---------------------------------------------------------------------------
# Transcription API
# ---------------------------------------------------------------------------


class TestTranscribeEndpoint:
    """POST /api/transcribe endpoint (fallback 200 path — no target)."""

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
                    target="",
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
                    target="",
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
                    target="",
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
                    target="",
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
                    target="",
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
                    target="",
                )
            )

        assert mock_transcribe.call_args[0][1] == "en"


class TestTranscribeSizeLimit:
    """POST /api/transcribe rejects audio exceeding 100 MB."""

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
                    target="",
                )
            )
        assert result.status_code == 200


class TestTranscribeServerSideInjection:
    """POST /api/transcribe with a valid target (202 path) and the fallback."""

    def _make_file(self, data=b"fake audio", filename="recording.webm"):
        f = mock.AsyncMock()
        f.filename = filename
        f.read = mock.AsyncMock(return_value=data)
        return f

    async def _drain(self):
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def test_valid_target_returns_202_and_schedules(self):
        """A valid target answers 202 and schedules the injection with that
        exact target, language and auto_enter, on the saved audio file."""
        captured = {}

        async def fake_inject(tmp_path, language, target, auto_enter):
            captured["args"] = (language, target, auto_enter)
            captured["existed"] = os.path.exists(tmp_path)
            os.unlink(tmp_path)

        async def run():
            with mock.patch(
                "terminal.routes._transcribe_and_inject", side_effect=fake_inject
            ):
                result = await tr.transcribe_audio(
                    file=self._make_file(),
                    language="fr",
                    auto_enter="true",
                    target="merlin:@12",
                )
                await self._drain()
                return result

        result = asyncio.run(run())
        assert result.status_code == 202
        assert json.loads(result.body)["status"] == "accepted"
        assert captured["args"] == ("fr", "merlin:@12", True)
        assert captured["existed"] is True

    @pytest.mark.parametrize("bad", ["", "merlin", "merlin:12", "mer lin:@1", ":@1"])
    def test_missing_or_malformed_target_falls_back(self, bad):
        """No target, or one that is not session:@id, takes the 200 path and
        schedules no injection."""
        with (
            mock.patch("transcribe.transcribe", return_value="hello"),
            mock.patch("os.unlink"),
            mock.patch("terminal.routes._transcribe_and_inject") as inject,
        ):
            result = asyncio.run(
                tr.transcribe_audio(
                    file=self._make_file(),
                    language="en",
                    auto_enter="false",
                    target=bad,
                )
            )
        assert result.status_code == 200
        assert json.loads(result.body)["text"] == "hello"
        inject.assert_not_called()


class TestTranscribeAndInject:
    """_transcribe_and_inject drives board.sweep to reach the target window,
    with the tmux helpers patched (their argv is checked in test_board_sweep)."""

    def _run(
        self,
        *,
        text="hello",
        target="merlin:@12",
        auto_enter=False,
        send_ok=True,
        transcribe_exc=None,
    ):
        if transcribe_exc is not None:
            cm_transcribe = mock.patch(
                "transcribe.transcribe", side_effect=transcribe_exc
            )
        else:
            cm_transcribe = mock.patch("transcribe.transcribe", return_value=text)
        with (
            cm_transcribe,
            mock.patch("board.sweep.exit_copy_mode") as exit_cm,
            mock.patch("board.sweep.send_literal", return_value=send_ok) as send_lit,
            mock.patch("board.sweep.send_enter", return_value=True) as send_enter,
            mock.patch("terminal.routes._unlink_safe") as unlink,
            mock.patch.object(tr.logger, "warning") as warn,
        ):
            asyncio.run(
                tr._transcribe_and_inject("/tmp/audio.webm", "en", target, auto_enter)
            )
        return {
            "exit_cm": exit_cm,
            "send_lit": send_lit,
            "send_enter": send_enter,
            "unlink": unlink,
            "warn": warn,
        }

    def test_sends_text_to_target_window(self):
        r = self._run(text="git status", target="merlin:@7")
        r["exit_cm"].assert_called_once_with("merlin:@7")
        r["send_lit"].assert_called_once_with("merlin:@7", "git status")
        r["send_enter"].assert_not_called()
        r["warn"].assert_not_called()
        r["unlink"].assert_called_once_with("/tmp/audio.webm")

    def test_auto_enter_sends_enter_after_text(self):
        r = self._run(auto_enter=True)
        r["send_lit"].assert_called_once()
        r["send_enter"].assert_called_once_with("merlin:@12")
        r["warn"].assert_not_called()

    def test_failed_send_logs_and_skips_enter(self):
        r = self._run(auto_enter=True, send_ok=False)
        r["send_lit"].assert_called_once()
        r["send_enter"].assert_not_called()
        r["warn"].assert_called_once()
        r["unlink"].assert_called_once_with("/tmp/audio.webm")

    def test_empty_transcription_writes_nothing(self):
        r = self._run(text="", auto_enter=True)
        r["exit_cm"].assert_not_called()
        r["send_lit"].assert_not_called()
        r["send_enter"].assert_not_called()
        r["unlink"].assert_called_once_with("/tmp/audio.webm")

    def test_temp_file_cleaned_on_transcribe_error(self):
        r = self._run(transcribe_exc=RuntimeError("boom"))
        r["send_lit"].assert_not_called()
        r["unlink"].assert_called_once_with("/tmp/audio.webm")


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

                result = await tr.api_terminal_cwd()

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

                result = await tr.api_terminal_cwd()

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

                result = await tr.api_terminal_cwd()

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
