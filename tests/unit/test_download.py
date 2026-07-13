"""Tests for the file download endpoint (POST /api/files/download)."""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from files.routes import api_router


@pytest.fixture()
def client():
    from fastapi import FastAPI

    app = FastAPI()
    # Mount as the framework does: api_router under /api/files.
    app.include_router(api_router, prefix="/api/files")
    return TestClient(app)


@pytest.fixture()
def sample_tree(tmp_path):
    """Create a sample directory tree for download tests.

    tmp_path/
      project/
        src/
          main.py    ("print('hello')")
          utils.py   ("# utils")
        README.md    ("# Project")
        data.bin     (binary: 256 bytes)
      single.txt     ("single file content")
      empty_dir/
    """
    project = tmp_path / "project"
    src = project / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text("print('hello')")
    (src / "utils.py").write_text("# utils")
    (project / "README.md").write_text("# Project")
    (project / "data.bin").write_bytes(bytes(range(256)))

    (tmp_path / "single.txt").write_text("single file content")

    (tmp_path / "empty_dir").mkdir()

    return tmp_path


# ---------------------------------------------------------------------------
# Single file download (no zip)
# ---------------------------------------------------------------------------


class TestDownloadSingleFile:
    def test_returns_file_directly(self, client, sample_tree):
        resp = client.post(
            "/api/files/download",
            json={"paths": [str(sample_tree / "single.txt")]},
        )
        assert resp.status_code == 200
        assert resp.text == "single file content"
        assert "single.txt" in resp.headers.get("content-disposition", "")

    def test_binary_file(self, client, sample_tree):
        resp = client.post(
            "/api/files/download",
            json={"paths": [str(sample_tree / "project" / "data.bin")]},
        )
        assert resp.status_code == 200
        assert resp.content == bytes(range(256))


# ---------------------------------------------------------------------------
# Single directory download (zip)
# ---------------------------------------------------------------------------


class TestDownloadDirectory:
    def test_single_directory_returns_zip(self, client, sample_tree):
        resp = client.post(
            "/api/files/download",
            json={"paths": [str(sample_tree / "project")]},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "project.zip" in resp.headers["content-disposition"]

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = sorted(zf.namelist())
        assert "project/README.md" in names
        assert "project/data.bin" in names
        assert "project/src/main.py" in names
        assert "project/src/utils.py" in names

    def test_zip_preserves_content(self, client, sample_tree):
        resp = client.post(
            "/api/files/download",
            json={"paths": [str(sample_tree / "project")]},
        )
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert zf.read("project/src/main.py") == b"print('hello')"
        assert zf.read("project/data.bin") == bytes(range(256))

    def test_zip_preserves_nested_structure(self, client, sample_tree):
        resp = client.post(
            "/api/files/download",
            json={"paths": [str(sample_tree / "project")]},
        )
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        # All entries should be under project/
        for name in zf.namelist():
            assert name.startswith("project/")

    def test_empty_directory_returns_empty_zip(self, client, sample_tree):
        resp = client.post(
            "/api/files/download",
            json={"paths": [str(sample_tree / "empty_dir")]},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert len(zf.namelist()) == 0


# ---------------------------------------------------------------------------
# Multi-file download (zip)
# ---------------------------------------------------------------------------


class TestDownloadMultiple:
    def test_multiple_files(self, client, sample_tree):
        paths = [
            str(sample_tree / "single.txt"),
            str(sample_tree / "project" / "README.md"),
        ]
        resp = client.post("/api/files/download", json={"paths": paths})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        assert "single.txt" in names
        assert "project/README.md" in names

    def test_mixed_files_and_dirs(self, client, sample_tree):
        paths = [
            str(sample_tree / "single.txt"),
            str(sample_tree / "project"),
        ]
        resp = client.post("/api/files/download", json={"paths": paths})
        assert resp.status_code == 200

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = sorted(zf.namelist())
        assert "single.txt" in names
        assert "project/src/main.py" in names
        assert "project/README.md" in names

    def test_zip_filename_uses_parent_name(self, client, sample_tree):
        paths = [
            str(sample_tree / "single.txt"),
            str(sample_tree / "project"),
        ]
        resp = client.post("/api/files/download", json={"paths": paths})
        parent_name = sample_tree.name
        assert f"{parent_name}.zip" in resp.headers["content-disposition"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestDownloadErrors:
    def test_empty_paths(self, client):
        resp = client.post("/api/files/download", json={"paths": []})
        assert resp.status_code == 400

    def test_nonexistent_path(self, client, tmp_path):
        resp = client.post(
            "/api/files/download",
            json={"paths": [str(tmp_path / "nonexistent")]},
        )
        assert resp.status_code == 404

    def test_blocked_path(self, client):
        resp = client.post(
            "/api/files/download",
            json={"paths": ["/proc/self/status"]},
        )
        assert resp.status_code == 403

    def test_blocked_path_in_multi(self, client, sample_tree):
        """One blocked path in a multi-path request → 403."""
        resp = client.post(
            "/api/files/download",
            json={
                "paths": [
                    str(sample_tree / "single.txt"),
                    "/proc/self/status",
                ]
            },
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Symlinks
# ---------------------------------------------------------------------------


class TestDownloadSymlinks:
    def test_symlink_to_valid_file_included(self, client, sample_tree):
        """Symlink to a valid file inside a directory is included in the zip."""
        link = sample_tree / "project" / "link.txt"
        link.symlink_to(sample_tree / "single.txt")

        resp = client.post(
            "/api/files/download",
            json={"paths": [str(sample_tree / "project")]},
        )
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert "project/link.txt" in zf.namelist()
        assert zf.read("project/link.txt") == b"single file content"

    def test_symlink_to_blocked_path_excluded(self, client, sample_tree):
        """Symlink to /proc inside a directory is excluded from the zip."""
        link = sample_tree / "project" / "bad_link"
        try:
            link.symlink_to("/proc/self/status")
        except (PermissionError, OSError):
            pytest.skip("Cannot create symlink to /proc")

        resp = client.post(
            "/api/files/download",
            json={"paths": [str(sample_tree / "project")]},
        )
        assert resp.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert "project/bad_link" not in zf.namelist()
