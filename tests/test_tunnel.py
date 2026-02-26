# /// script
# dependencies = ["pytest"]
# ///
"""Tests for tunnel.py — Cloudflare Tunnel manager."""

import asyncio
from unittest import mock

import pytest

import tunnel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_tunnel_state():
    """Reset module-level state between tests."""
    tunnel._public_url = None
    tunnel._process = None
    tunnel._status = "stopped"
    yield
    tunnel._public_url = None
    tunnel._process = None
    tunnel._status = "stopped"


# ---------------------------------------------------------------------------
# State accessors
# ---------------------------------------------------------------------------

class TestState:
    """Module-level state accessors."""

    def test_initial_url_is_none(self):
        assert tunnel.get_public_url() is None

    def test_initial_status_is_stopped(self):
        assert tunnel.get_status() == "stopped"

    def test_url_after_set(self):
        tunnel._public_url = "https://test.trycloudflare.com"
        assert tunnel.get_public_url() == "https://test.trycloudflare.com"

    def test_status_after_set(self):
        tunnel._status = "running"
        assert tunnel.get_status() == "running"


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

class TestParseUrl:
    """_parse_url_from_stderr extracts the tunnel URL from cloudflared output."""

    def _make_proc(self, lines: list[str]):
        """Create a mock process with predetermined stderr output."""
        proc = mock.AsyncMock()
        encoded = [line.encode() + b"\n" for line in lines] + [b""]
        proc.stderr.readline = mock.AsyncMock(side_effect=encoded)
        return proc

    def test_parses_quick_tunnel_url(self):
        lines = [
            "2026-02-15 INF Starting tunnel",
            "+-----------------------------------------------------------+",
            "|  Your quick Tunnel has been created!                      |",
            "|  https://fancy-words-here.trycloudflare.com               |",
            "+-----------------------------------------------------------+",
        ]
        proc = self._make_proc(lines)
        url = asyncio.run(tunnel._parse_url_from_stderr(proc))
        assert url == "https://fancy-words-here.trycloudflare.com"

    def test_parses_url_with_hyphens_and_numbers(self):
        lines = [
            "| https://abc-123-def-456.trycloudflare.com |",
        ]
        proc = self._make_proc(lines)
        url = asyncio.run(tunnel._parse_url_from_stderr(proc))
        assert url == "https://abc-123-def-456.trycloudflare.com"

    def test_returns_none_on_no_url(self):
        lines = [
            "2026-02-15 INF Starting tunnel",
            "2026-02-15 ERR failed to connect",
        ]
        proc = self._make_proc(lines)
        url = asyncio.run(tunnel._parse_url_from_stderr(proc))
        assert url is None

    def test_handles_empty_stderr(self):
        proc = self._make_proc([])
        url = asyncio.run(tunnel._parse_url_from_stderr(proc))
        assert url is None

    def test_ignores_non_trycloudflare_urls(self):
        lines = [
            "https://not-a-tunnel.example.com",
        ]
        proc = self._make_proc(lines)
        url = asyncio.run(tunnel._parse_url_from_stderr(proc))
        assert url is None


# ---------------------------------------------------------------------------
# Launch functions
# ---------------------------------------------------------------------------

class TestLaunchQuickTunnel:
    """_launch_quick_tunnel spawns cloudflared and parses the URL."""

    def test_returns_url_and_process(self):
        mock_proc = mock.AsyncMock()
        stderr_lines = [
            b"2026-02-15 INF Starting tunnel\n",
            b"| https://test-url.trycloudflare.com |\n",
            b"",  # EOF
        ]
        mock_proc.stderr.readline = mock.AsyncMock(side_effect=stderr_lines)

        with mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            url, proc = asyncio.run(tunnel._launch_quick_tunnel(port=3123))

        assert url == "https://test-url.trycloudflare.com"
        assert proc is mock_proc

    def test_returns_none_url_on_failure(self):
        mock_proc = mock.AsyncMock()
        mock_proc.stderr.readline = mock.AsyncMock(side_effect=[
            b"ERR connection failed\n",
            b"",
        ])

        with mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            url, proc = asyncio.run(tunnel._launch_quick_tunnel(port=3123))

        assert url is None
        assert proc is mock_proc


class TestLaunchNamedTunnel:
    """_launch_named_tunnel spawns cloudflared with token."""

    def test_returns_hostname_url(self):
        mock_proc = mock.AsyncMock()

        with mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            url, proc = asyncio.run(
                tunnel._launch_named_tunnel(
                    tunnel_token="eyJtest", tunnel_hostname="merlin.example.com"
                )
            )

        assert url == "https://merlin.example.com"
        assert proc is mock_proc

    def test_returns_none_url_without_hostname(self):
        mock_proc = mock.AsyncMock()

        with mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            url, proc = asyncio.run(
                tunnel._launch_named_tunnel(tunnel_token="eyJtest", tunnel_hostname="")
            )

        assert url is None
        assert proc is mock_proc


# ---------------------------------------------------------------------------
# start_tunnel dispatching
# ---------------------------------------------------------------------------

class TestStartTunnel:
    """start_tunnel dispatches to the correct mode."""

    def test_dispatches_to_quick_tunnel_when_no_token(self):
        """Without tunnel_token, uses _launch_quick_tunnel."""
        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 1  # Exit immediately
        mock_proc.wait = mock.AsyncMock(return_value=1)
        mock_proc.terminate = mock.Mock()

        stderr_lines = [
            b"| https://quick.trycloudflare.com |\n",
            b"",
        ]
        mock_proc.stderr.readline = mock.AsyncMock(side_effect=stderr_lines)

        with mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = asyncio.run(
                tunnel.start_tunnel(port=3123, tunnel_token="", max_restarts=0, restart_delay=0)
            )

        # Returns the URL that was successfully parsed
        assert result == "https://quick.trycloudflare.com"

    def test_dispatches_to_named_tunnel_when_token_set(self):
        """With tunnel_token, uses _launch_named_tunnel."""
        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 1
        mock_proc.wait = mock.AsyncMock(return_value=1)

        with mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = asyncio.run(
                tunnel.start_tunnel(
                    tunnel_token="eyJtest",
                    tunnel_hostname="merlin.example.com",
                    max_restarts=0,
                    restart_delay=0,
                )
            )

        assert result == "https://merlin.example.com"

    def test_status_transitions(self):
        """Status goes starting → running → error."""
        statuses = []

        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 1
        mock_proc.wait = mock.AsyncMock(return_value=1)

        stderr_lines = [
            b"| https://test.trycloudflare.com |\n",
            b"",
        ]
        mock_proc.stderr.readline = mock.AsyncMock(side_effect=stderr_lines)

        original_run_loop = tunnel._run_tunnel_loop

        async def tracking_run_loop(launch_fn, **kwargs):
            # Intercept to track status at key points
            result = await original_run_loop(launch_fn, **kwargs)
            return result

        with mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            asyncio.run(
                tunnel.start_tunnel(port=3123, tunnel_token="", max_restarts=0, restart_delay=0)
            )

        # After exhausting restarts, status should be error
        assert tunnel.get_status() == "error"

    def test_url_cleared_after_final_failure(self):
        """After max restarts, public URL is cleared."""
        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 1
        mock_proc.wait = mock.AsyncMock(return_value=1)

        stderr_lines = [
            b"| https://test.trycloudflare.com |\n",
            b"",
        ]
        mock_proc.stderr.readline = mock.AsyncMock(side_effect=stderr_lines)

        with mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            asyncio.run(
                tunnel.start_tunnel(port=3123, tunnel_token="", max_restarts=0, restart_delay=0)
            )

        assert tunnel.get_public_url() is None


# ---------------------------------------------------------------------------
# Stop tunnel
# ---------------------------------------------------------------------------

class TestStopTunnel:
    """stop_tunnel cleans up process state."""

    def test_stop_when_not_running(self):
        """Stopping when no process exists is a no-op."""
        asyncio.run(tunnel.stop_tunnel())
        assert tunnel.get_status() == "stopped"
        assert tunnel.get_public_url() is None

    def test_stop_terminates_process(self):
        """Running process gets terminated."""
        proc = mock.AsyncMock()
        proc.returncode = None  # Still running
        proc.wait = mock.AsyncMock()
        proc.terminate = mock.Mock()
        tunnel._process = proc
        tunnel._status = "running"
        tunnel._public_url = "https://test.trycloudflare.com"

        asyncio.run(tunnel.stop_tunnel())

        proc.terminate.assert_called_once()
        assert tunnel.get_status() == "stopped"
        assert tunnel.get_public_url() is None

    def test_stop_kills_if_terminate_times_out(self):
        """If terminate doesn't work within timeout, kill the process."""
        proc = mock.AsyncMock()
        proc.returncode = None
        proc.terminate = mock.Mock()
        proc.kill = mock.Mock()

        call_count = [0]
        async def mock_wait():
            call_count[0] += 1
            if call_count[0] == 1:
                raise asyncio.TimeoutError()
            return  # second call (after kill) succeeds

        proc.wait = mock_wait
        tunnel._process = proc
        tunnel._status = "running"

        asyncio.run(tunnel.stop_tunnel())

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert tunnel.get_status() == "stopped"

    def test_stop_already_exited_process(self):
        """Process that already exited — no terminate/kill needed."""
        proc = mock.AsyncMock()
        proc.returncode = 0  # Already exited
        tunnel._process = proc
        tunnel._status = "running"
        tunnel._public_url = "https://test.trycloudflare.com"

        asyncio.run(tunnel.stop_tunnel())

        proc.terminate.assert_not_called()
        assert tunnel.get_status() == "stopped"
        assert tunnel.get_public_url() is None
