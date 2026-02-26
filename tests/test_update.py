"""Tests for merlin update — version comparison, symlink flip, download mock."""

import io
import json
import os
import tarfile
from pathlib import Path
from unittest import mock

import pytest

import paths
from cli import (
    atomic_symlink,
    download_and_extract,
    fetch_latest_tag,
    get_version,
    run_update,
)


@pytest.fixture(autouse=True)
def _reset_paths():
    paths._dev_mode_override = None
    yield
    paths._dev_mode_override = None


# ---------------------------------------------------------------------------
# atomic_symlink
# ---------------------------------------------------------------------------


class TestAtomicSymlink:
    def test_creates_symlink(self, tmp_path):
        target = tmp_path / "versions" / "0.2.0"
        target.mkdir(parents=True)
        link = tmp_path / "current"

        atomic_symlink(target, link)

        assert link.is_symlink()
        assert link.resolve() == target

    def test_replaces_existing_symlink(self, tmp_path):
        old = tmp_path / "versions" / "0.1.0"
        new = tmp_path / "versions" / "0.2.0"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        link = tmp_path / "current"

        # Create initial symlink
        link.symlink_to(old)
        assert link.resolve() == old

        # Swap to new
        atomic_symlink(new, link)
        assert link.resolve() == new

    def test_old_version_preserved(self, tmp_path):
        old = tmp_path / "versions" / "0.1.0"
        new = tmp_path / "versions" / "0.2.0"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        link = tmp_path / "current"

        link.symlink_to(old)
        atomic_symlink(new, link)

        # Old version directory still exists
        assert old.exists()


# ---------------------------------------------------------------------------
# fetch_latest_tag (mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchLatestTag:
    def _mock_tags_response(self, tags_json):
        """Helper to mock urlopen returning a tags API response."""
        response = json.dumps(tags_json).encode()
        m = mock.Mock()
        m.return_value.__enter__ = mock.Mock(return_value=mock.Mock(read=mock.Mock(return_value=response)))
        m.return_value.__exit__ = mock.Mock(return_value=False)
        return m

    def test_parses_tag(self):
        with mock.patch("urllib.request.urlopen", self._mock_tags_response([{"name": "v0.3.0"}])):
            assert fetch_latest_tag() == "0.3.0"

    def test_strips_v_prefix(self):
        with mock.patch("urllib.request.urlopen", self._mock_tags_response([{"name": "v1.0.0"}])):
            assert fetch_latest_tag() == "1.0.0"

    def test_handles_no_v_prefix(self):
        with mock.patch("urllib.request.urlopen", self._mock_tags_response([{"name": "2.0.0"}])):
            assert fetch_latest_tag() == "2.0.0"

    def test_returns_first_tag(self):
        tags = [{"name": "v0.3.0"}, {"name": "v0.2.0"}, {"name": "v0.1.0"}]
        with mock.patch("urllib.request.urlopen", self._mock_tags_response(tags)):
            assert fetch_latest_tag() == "0.3.0"

    def test_returns_none_on_network_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=Exception("Network error")):
            assert fetch_latest_tag() is None

    def test_returns_none_on_empty_tags(self):
        with mock.patch("urllib.request.urlopen", self._mock_tags_response([])):
            assert fetch_latest_tag() is None


# ---------------------------------------------------------------------------
# run_update (integration with mocks)
# ---------------------------------------------------------------------------


class TestRunUpdate:
    def test_already_up_to_date(self, tmp_path, monkeypatch, capsys):
        paths.set_dev_mode(False)
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))

        # Set up current version
        versions_dir = tmp_path / "versions" / "0.3.0"
        versions_dir.mkdir(parents=True)
        current = tmp_path / "current"
        current.symlink_to(versions_dir)

        with mock.patch("cli.fetch_latest_tag", return_value="0.3.0"):
            run_update()

        output = capsys.readouterr().out
        assert "Already up to date" in output

    def test_update_to_new_version(self, tmp_path, monkeypatch, capsys):
        paths.set_dev_mode(False)
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))

        # Set up current version
        old_dir = tmp_path / "versions" / "0.2.0"
        old_dir.mkdir(parents=True)
        current = tmp_path / "current"
        current.symlink_to(old_dir)

        # Pre-create the "downloaded" new version
        new_dir = tmp_path / "versions" / "0.3.0"
        new_dir.mkdir(parents=True)

        with mock.patch("cli.fetch_latest_tag", return_value="0.3.0"), \
             mock.patch("cli.download_and_extract"):
            run_update()

        output = capsys.readouterr().out
        assert "0.2.0" in output
        assert "0.3.0" in output
        assert "Updated" in output

        # Symlink should now point to new version
        assert current.resolve() == new_dir

    def test_update_fails_if_no_latest(self, tmp_path, monkeypatch):
        paths.set_dev_mode(False)
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))

        versions_dir = tmp_path / "versions" / "0.1.0"
        versions_dir.mkdir(parents=True)
        current = tmp_path / "current"
        current.symlink_to(versions_dir)

        with mock.patch("cli.fetch_latest_tag", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                run_update()
            assert exc_info.value.code == 1

    def test_old_version_kept(self, tmp_path, monkeypatch):
        """Old version directory is kept for manual rollback."""
        paths.set_dev_mode(False)
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))

        old_dir = tmp_path / "versions" / "0.1.0"
        old_dir.mkdir(parents=True)
        (old_dir / "marker.txt").write_text("old")

        new_dir = tmp_path / "versions" / "0.2.0"
        new_dir.mkdir(parents=True)

        current = tmp_path / "current"
        current.symlink_to(old_dir)

        with mock.patch("cli.fetch_latest_tag", return_value="0.2.0"), \
             mock.patch("cli.download_and_extract"):
            run_update()

        # Old version still exists with its content
        assert old_dir.exists()
        assert (old_dir / "marker.txt").read_text() == "old"


# ---------------------------------------------------------------------------
# Version detection for update context
# ---------------------------------------------------------------------------


class TestVersionForUpdate:
    def test_reads_symlink_folder_name(self, tmp_path, monkeypatch):
        paths.set_dev_mode(False)
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))

        versions_dir = tmp_path / "versions" / "0.5.0"
        versions_dir.mkdir(parents=True)
        current = tmp_path / "current"
        current.symlink_to(versions_dir)

        assert get_version() == "0.5.0"

    def test_manual_rollback_scenario(self, tmp_path, monkeypatch):
        """Simulate manual rollback: re-symlink to old version."""
        paths.set_dev_mode(False)
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))

        old = tmp_path / "versions" / "0.1.0"
        new = tmp_path / "versions" / "0.2.0"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        current = tmp_path / "current"

        # Start on new version
        current.symlink_to(new)
        assert get_version() == "0.2.0"

        # Rollback to old
        atomic_symlink(old, current)
        assert get_version() == "0.1.0"


# ---------------------------------------------------------------------------
# download_and_extract
# ---------------------------------------------------------------------------


def _make_tarball(tmp_path, members: dict, prefix: str = "merlin-0.1.0") -> Path:
    """Create a test tarball. members = {relative_path: content}."""
    tarball_path = tmp_path / "test.tar.gz"
    with tarfile.open(tarball_path, "w:gz") as tar:
        # Add top-level dir
        d = tarfile.TarInfo(name=prefix)
        d.type = tarfile.DIRTYPE
        tar.addfile(d)
        for name, content in members.items():
            info = tarfile.TarInfo(name=f"{prefix}/{name}")
            data = content.encode() if isinstance(content, str) else content
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return tarball_path


def _mock_urlopen(tarball_path: Path):
    """Return a mock urlopen context manager that serves a local file."""
    def fake_urlopen(req, **kwargs):
        data = tarball_path.read_bytes()
        return mock.Mock(
            __enter__=mock.Mock(return_value=io.BytesIO(data)),
            __exit__=mock.Mock(return_value=False),
        )
    return fake_urlopen


class TestDownloadAndExtract:
    def test_extracts_tarball(self, tmp_path):
        """Normal extraction strips prefix and creates files."""
        tarball = _make_tarball(tmp_path, {"main.py": "print('hello')", "cli.py": "# cli"})
        target = tmp_path / "extracted"

        with mock.patch("urllib.request.urlopen", side_effect=_mock_urlopen(tarball)):
            download_and_extract("0.1.0", target)

        assert (target / "main.py").read_text() == "print('hello')"
        assert (target / "cli.py").read_text() == "# cli"

    def test_rejects_path_traversal(self, tmp_path):
        """Tarball entries with ../ are rejected."""
        tarball_path = tmp_path / "evil.tar.gz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            d = tarfile.TarInfo(name="merlin-0.1.0")
            d.type = tarfile.DIRTYPE
            tar.addfile(d)
            # Normal file
            info = tarfile.TarInfo(name="merlin-0.1.0/ok.txt")
            info.size = 2
            tar.addfile(info, io.BytesIO(b"ok"))
            # Malicious path traversal
            info = tarfile.TarInfo(name="merlin-0.1.0/../../etc/evil")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"evil"))

        target = tmp_path / "extracted"
        with mock.patch("urllib.request.urlopen", side_effect=_mock_urlopen(tarball_path)):
            with pytest.raises(ValueError, match="Path traversal"):
                download_and_extract("0.1.0", target)

        # Target should not exist (cleaned up on failure)
        assert not target.exists()

    def test_rejects_symlinks_in_tarball(self, tmp_path):
        """Symlink entries in tarballs are silently skipped."""
        tarball_path = tmp_path / "symlink.tar.gz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            d = tarfile.TarInfo(name="merlin-0.1.0")
            d.type = tarfile.DIRTYPE
            tar.addfile(d)
            # Regular file
            info = tarfile.TarInfo(name="merlin-0.1.0/main.py")
            info.size = 5
            tar.addfile(info, io.BytesIO(b"hello"))
            # Symlink (should be skipped)
            sym = tarfile.TarInfo(name="merlin-0.1.0/link")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "/etc/passwd"
            tar.addfile(sym)

        target = tmp_path / "extracted"
        with mock.patch("urllib.request.urlopen", side_effect=_mock_urlopen(tarball_path)):
            download_and_extract("0.1.0", target)

        # Regular file extracted, symlink skipped
        assert (target / "main.py").exists()
        assert not (target / "link").exists()

    def test_cleans_up_on_download_failure(self, tmp_path):
        """Failed download leaves no staging directory behind."""
        target = tmp_path / "extracted"
        staging = tmp_path / ".extracted.downloading"

        with mock.patch("urllib.request.urlopen", side_effect=Exception("Network")):
            with pytest.raises(Exception, match="Network"):
                download_and_extract("0.1.0", target)

        assert not target.exists()
        assert not staging.exists()

    def test_cleans_up_on_corrupt_tarball(self, tmp_path):
        """Corrupt tarball leaves no partial extraction."""
        corrupt = tmp_path / "corrupt.tar.gz"
        corrupt.write_bytes(b"not a tarball")

        target = tmp_path / "extracted"
        with mock.patch("urllib.request.urlopen", side_effect=_mock_urlopen(corrupt)):
            with pytest.raises(Exception):
                download_and_extract("0.1.0", target)

        assert not target.exists()


# ---------------------------------------------------------------------------
# Version comparison in run_update
# ---------------------------------------------------------------------------


class TestUpdateVersionComparison:
    def test_dev_suffix_does_not_trigger_update(self, tmp_path, monkeypatch, capsys):
        """Dev versions like '0.3.0-3-gabcdef' match base version '0.3.0' — no update needed."""
        paths.set_dev_mode(False)
        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))

        v_dir = tmp_path / "versions" / "0.3.0"
        v_dir.mkdir(parents=True)
        current = tmp_path / "current"
        current.symlink_to(v_dir)

        with mock.patch("cli.fetch_latest_tag", return_value="0.3.0"), \
             mock.patch("cli.get_version", return_value="0.3.0-3-gabcdef"):
            run_update()

        output = capsys.readouterr().out
        assert "Already up to date" in output
