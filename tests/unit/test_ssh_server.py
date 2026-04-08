"""Tests for ssh_server.py — Container-side SSH server."""

import asyncio
import os
from pathlib import Path

import asyncssh
import pytest

import ssh_server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Reset module-level state between tests."""
    ssh_server._server = None
    yield
    # Ensure server is stopped after each test
    if ssh_server._server is not None:
        try:
            asyncio.get_event_loop().run_until_complete(ssh_server.stop_ssh_server())
        except Exception:
            ssh_server._server = None


@pytest.fixture
def tmp_merlin_home(tmp_path):
    """Temporary MERLIN_HOME for isolated host key tests."""
    old_home = os.environ.get("MERLIN_HOME")
    os.environ["MERLIN_HOME"] = str(tmp_path)
    yield tmp_path
    if old_home is not None:
        os.environ["MERLIN_HOME"] = old_home
    else:
        os.environ.pop("MERLIN_HOME", None)


def _free_port() -> int:
    """Find a free port for testing."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Host key tests
# ---------------------------------------------------------------------------


class TestHostKey:
    """Host key generation and persistence."""

    def test_host_key_created_if_missing(self, tmp_merlin_home):
        """No key file -> new key generated."""
        key_path = tmp_merlin_home / "ssh" / "host_key"
        assert not key_path.exists()

        result = ssh_server._ensure_host_key()
        assert key_path.exists()
        assert result == str(key_path)

        # Verify it's a valid Ed25519 key
        key = asyncssh.read_private_key(result)
        algo = key.get_algorithm()
        assert (algo.decode() if isinstance(algo, bytes) else algo) == "ssh-ed25519"

    def test_host_key_persistence(self, tmp_merlin_home):
        """Generates key, restart, same key loaded."""
        # First call — generate
        path1 = ssh_server._ensure_host_key()
        key1 = asyncssh.read_private_key(path1)

        # Second call — should load existing (not regenerated)
        path2 = ssh_server._ensure_host_key()
        key2 = asyncssh.read_private_key(path2)

        assert path1 == path2
        # Compare public keys (private export includes random padding)
        assert key1.export_public_key() == key2.export_public_key()

    def test_host_key_permissions(self, tmp_merlin_home):
        """Generated key file has 0600 permissions."""
        path = ssh_server._ensure_host_key()
        mode = oct(Path(path).stat().st_mode & 0o777)
        assert mode == "0o600"


# ---------------------------------------------------------------------------
# Server start/stop tests
# ---------------------------------------------------------------------------


class TestServerStartStop:
    """SSH server lifecycle."""

    def test_ssh_server_starts(self, tmp_merlin_home):
        """Server starts and accepts connections."""
        port = _free_port()

        async def _test():
            acceptor = await ssh_server.start_ssh_server(port=port)
            assert acceptor is not None
            assert ssh_server._server is not None

            # Connect to verify it's listening
            async with asyncssh.connect(
                "127.0.0.1",
                port=port,
                known_hosts=None,
                username="test",
                password="",
            ) as conn:
                assert conn is not None

            await ssh_server.stop_ssh_server()
            assert ssh_server._server is None

        asyncio.run(_test())

    def test_ssh_server_no_auth(self, tmp_merlin_home):
        """Connection accepted without credentials."""
        port = _free_port()

        async def _test():
            acceptor = await ssh_server.start_ssh_server(port=port)
            assert acceptor is not None

            # Connect with no password and no keys
            async with asyncssh.connect(
                "127.0.0.1",
                port=port,
                known_hosts=None,
                username="anyuser",
                password="",
            ) as conn:
                assert conn is not None

            await ssh_server.stop_ssh_server()

        asyncio.run(_test())

    def test_ssh_server_port_unavailable(self, tmp_merlin_home):
        """If port is in use, returns None gracefully."""
        port = _free_port()

        async def _test():
            # Start first server
            s1 = await ssh_server.start_ssh_server(port=port)
            assert s1 is not None

            # Reset module state so start_ssh_server doesn't see existing server
            saved = ssh_server._server
            ssh_server._server = None

            # Try to start second on same port — should fail gracefully
            s2 = await ssh_server.start_ssh_server(port=port)
            assert s2 is None

            # Cleanup
            ssh_server._server = saved
            await ssh_server.stop_ssh_server()

        asyncio.run(_test())

    def test_ssh_server_stop_when_not_running(self):
        """Stopping when no server is running is a no-op."""

        async def _test():
            ssh_server._server = None
            await ssh_server.stop_ssh_server()
            assert ssh_server._server is None

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# SFTP tests
# ---------------------------------------------------------------------------


class TestSFTP:
    """SFTP access."""

    def test_sftp_list_directory(self, tmp_merlin_home):
        """Connect via SFTP, list directory, verify files visible."""
        port = _free_port()

        async def _test():
            acceptor = await ssh_server.start_ssh_server(port=port)
            assert acceptor is not None

            async with asyncssh.connect(
                "127.0.0.1",
                port=port,
                known_hosts=None,
                username="test",
                password="",
            ) as conn:
                async with conn.start_sftp_client() as sftp:
                    # List /tmp — should always exist
                    entries = await sftp.listdir("/tmp")
                    assert isinstance(entries, list)

            await ssh_server.stop_ssh_server()

        asyncio.run(_test())

    def test_sftp_read_write_file(self, tmp_merlin_home, tmp_path):
        """SFTP can write and read back a file."""
        port = _free_port()
        test_file = tmp_path / "sftp_test.txt"
        test_content = "hello from sftp test"

        async def _test():
            acceptor = await ssh_server.start_ssh_server(port=port)
            assert acceptor is not None

            async with asyncssh.connect(
                "127.0.0.1",
                port=port,
                known_hosts=None,
                username="test",
                password="",
            ) as conn:
                async with conn.start_sftp_client() as sftp:
                    # Write
                    async with sftp.open(str(test_file), "w") as f:
                        await f.write(test_content)

                    # Read back
                    async with sftp.open(str(test_file), "r") as f:
                        content = await f.read()

                    assert content == test_content

                    # Verify via SFTP stat
                    stat = await sftp.stat(str(test_file))
                    assert stat.size > 0

            await ssh_server.stop_ssh_server()

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# MerlinShellServer class tests
# ---------------------------------------------------------------------------


class TestMerlinShellServer:
    """MerlinShellServer behavior."""

    def test_begin_auth_returns_false(self):
        """begin_auth returns False (no auth required)."""
        server = ssh_server.MerlinShellServer()
        result = server.begin_auth("anyuser")
        assert result is False

    def test_begin_auth_any_username(self):
        """begin_auth returns False for any username."""
        server = ssh_server.MerlinShellServer()
        for name in ("root", "user", "admin", "", "test"):
            assert server.begin_auth(name) is False
