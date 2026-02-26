"""Tests for files/fs_helpers.py — path validation, directory listing, file reading."""

import os
import stat

import pytest

from files.fs_helpers import (
    BLOCKED_PREFIXES,
    IMAGE_EXTENSIONS,
    TEXT_MAX_BYTES,
    get_file_info,
    list_directory,
    read_text_file,
    validate_path,
    _is_text_file,
)


# ---------------------------------------------------------------------------
# validate_path
# ---------------------------------------------------------------------------


class TestValidatePath:
    def test_root_path(self):
        p = validate_path("/")
        assert str(p) == "/"

    def test_empty_string_defaults_to_root(self):
        p = validate_path("")
        assert str(p) == "/"

    def test_absolute_path(self):
        p = validate_path("/tmp")
        assert str(p) == "/tmp"

    def test_resolves_symlinks(self, tmp_path):
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        p = validate_path(str(link))
        assert p == target.resolve()

    def test_resolves_dot_dot(self, tmp_path):
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        p = validate_path(str(sub / ".."))
        assert p == (tmp_path / "a").resolve()

    def test_blocks_proc(self):
        with pytest.raises(ValueError, match="not allowed"):
            validate_path("/proc")

    def test_blocks_proc_subpath(self):
        with pytest.raises(ValueError, match="not allowed"):
            validate_path("/proc/self/status")

    def test_blocks_sys(self):
        with pytest.raises(ValueError, match="not allowed"):
            validate_path("/sys")

    def test_blocks_sys_subpath(self):
        with pytest.raises(ValueError, match="not allowed"):
            validate_path("/sys/class/net")

    def test_blocks_dev(self):
        with pytest.raises(ValueError, match="not allowed"):
            validate_path("/dev")

    def test_blocks_dev_subpath(self):
        with pytest.raises(ValueError, match="not allowed"):
            validate_path("/dev/null")

    def test_allows_normal_paths(self):
        # These should not raise
        validate_path("/tmp")
        validate_path("/home")
        validate_path("/usr/local/bin")

    def test_allows_paths_containing_proc_in_name(self):
        # /home/user/processor should be fine
        p = validate_path("/home/user/processor")
        assert "processor" in str(p)


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------


class TestListDirectory:
    def test_lists_files_and_dirs(self, tmp_path):
        (tmp_path / "file.txt").write_text("hello")
        (tmp_path / "subdir").mkdir()
        result = list_directory(tmp_path)

        assert result["type"] == "directory"
        assert len(result["entries"]) == 2

        # Directories come first
        assert result["entries"][0]["name"] == "subdir"
        assert result["entries"][0]["type"] == "dir"
        assert result["entries"][1]["name"] == "file.txt"
        assert result["entries"][1]["type"] == "file"

    def test_dir_entries_have_no_size(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        result = list_directory(tmp_path)
        assert result["entries"][0]["size"] is None

    def test_file_entries_have_size(self, tmp_path):
        (tmp_path / "file.txt").write_text("hello world")
        result = list_directory(tmp_path)
        assert result["entries"][0]["size"] == 11

    def test_file_entries_have_mtime(self, tmp_path):
        (tmp_path / "file.txt").write_text("hello")
        result = list_directory(tmp_path)
        assert result["entries"][0]["mtime"] is not None
        assert isinstance(result["entries"][0]["mtime"], float)

    def test_hidden_files_detected(self, tmp_path):
        (tmp_path / ".hidden").write_text("secret")
        (tmp_path / "visible").write_text("public")
        result = list_directory(tmp_path)

        hidden = [e for e in result["entries"] if e["is_hidden"]]
        visible = [e for e in result["entries"] if not e["is_hidden"]]
        assert len(hidden) == 1
        assert hidden[0]["name"] == ".hidden"
        assert len(visible) == 1

    def test_empty_directory(self, tmp_path):
        result = list_directory(tmp_path)
        assert result["entries"] == []

    def test_sorts_dirs_first_then_alpha(self, tmp_path):
        (tmp_path / "zebra.txt").write_text("")
        (tmp_path / "alpha.txt").write_text("")
        (tmp_path / "beta_dir").mkdir()
        (tmp_path / "alpha_dir").mkdir()

        result = list_directory(tmp_path)
        names = [e["name"] for e in result["entries"]]
        assert names == ["alpha_dir", "beta_dir", "alpha.txt", "zebra.txt"]

    def test_case_insensitive_sort(self, tmp_path):
        (tmp_path / "Banana.txt").write_text("")
        (tmp_path / "apple.txt").write_text("")
        result = list_directory(tmp_path)
        names = [e["name"] for e in result["entries"]]
        assert names == ["apple.txt", "Banana.txt"]

    def test_not_a_directory_raises(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hello")
        with pytest.raises(ValueError, match="Not a directory"):
            list_directory(f)

    def test_nonexistent_path_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            list_directory(tmp_path / "nonexistent")

    def test_permission_denied_on_dir(self, tmp_path):
        restricted = tmp_path / "restricted"
        restricted.mkdir()
        restricted.chmod(0o000)
        try:
            with pytest.raises(PermissionError):
                list_directory(restricted)
        finally:
            restricted.chmod(0o755)

    def test_inaccessible_child_listed_as_unknown(self, tmp_path):
        child = tmp_path / "noperm"
        child.write_text("data")
        child.chmod(0o000)
        try:
            result = list_directory(tmp_path)
            entry = result["entries"][0]
            # Should still be listed — stat might fail but we handle it
            assert entry["name"] == "noperm"
        finally:
            child.chmod(0o644)

    def test_path_included_in_result(self, tmp_path):
        result = list_directory(tmp_path)
        assert result["path"] == str(tmp_path)


# ---------------------------------------------------------------------------
# get_file_info
# ---------------------------------------------------------------------------


class TestGetFileInfo:
    def test_basic_text_file(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text("print('hello')")
        info = get_file_info(f)

        assert info["type"] == "file"
        assert info["name"] == "script.py"
        assert info["is_text"] is True
        assert info["is_image"] is False
        assert info["size"] > 0
        assert info["mtime"] is not None

    def test_image_file(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"\x89PNG\r\n")
        info = get_file_info(f)

        assert info["is_image"] is True
        assert info["is_text"] is False
        assert "image" in info["mime_type"]

    def test_binary_file(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00\x01\x02\x03")
        info = get_file_info(f)

        assert info["is_image"] is False
        assert info["is_text"] is False

    def test_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            get_file_info(tmp_path / "nope.txt")

    def test_directory_raises(self, tmp_path):
        with pytest.raises(ValueError, match="directory"):
            get_file_info(tmp_path)

    def test_json_file_is_text(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}')
        info = get_file_info(f)
        assert info["is_text"] is True

    def test_markdown_file_is_text(self, tmp_path):
        f = tmp_path / "README.md"
        f.write_text("# Hello")
        info = get_file_info(f)
        assert info["is_text"] is True

    def test_all_image_extensions_detected(self, tmp_path):
        for ext in IMAGE_EXTENSIONS:
            f = tmp_path / f"test{ext}"
            f.write_bytes(b"\x00")
            info = get_file_info(f)
            assert info["is_image"] is True, f"Expected {ext} to be detected as image"


# ---------------------------------------------------------------------------
# read_text_file
# ---------------------------------------------------------------------------


class TestReadTextFile:
    def test_reads_content(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\n")
        result = read_text_file(f)

        assert result["content"] == "line1\nline2\nline3\n"
        assert result["truncated"] is False
        assert result["line_count"] == 3
        assert result["size"] == len("line1\nline2\nline3\n")

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        result = read_text_file(f)

        assert result["content"] == ""
        assert result["line_count"] == 0
        assert result["truncated"] is False

    def test_single_line_no_newline(self, tmp_path):
        f = tmp_path / "one.txt"
        f.write_text("hello")
        result = read_text_file(f)

        assert result["content"] == "hello"
        assert result["line_count"] == 1

    def test_truncation_flag(self, tmp_path):
        f = tmp_path / "big.txt"
        # Write more than TEXT_MAX_BYTES
        f.write_text("x" * (TEXT_MAX_BYTES + 1000))
        result = read_text_file(f)

        assert result["truncated"] is True
        assert len(result["content"]) == TEXT_MAX_BYTES

    def test_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_text_file(tmp_path / "nope.txt")

    def test_permission_denied(self, tmp_path):
        f = tmp_path / "secret.txt"
        f.write_text("secret")
        f.chmod(0o000)
        try:
            with pytest.raises(PermissionError):
                read_text_file(f)
        finally:
            f.chmod(0o644)

    def test_binary_content_with_replace(self, tmp_path):
        f = tmp_path / "mixed.txt"
        f.write_bytes(b"hello\x00world\xff")
        result = read_text_file(f)
        # Should not raise — uses errors='replace'
        assert "hello" in result["content"]


# ---------------------------------------------------------------------------
# _is_text_file
# ---------------------------------------------------------------------------


class TestIsTextFile:
    def test_python_file(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("pass")
        assert _is_text_file(f, "text/x-python") is True

    def test_binary_file(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"\x00\x01\x02")
        assert _is_text_file(f, "application/octet-stream") is False

    def test_no_extension_text(self, tmp_path):
        f = tmp_path / "Makefile"
        f.write_text("all:\n\techo hello")
        assert _is_text_file(f, "application/octet-stream") is True

    def test_no_extension_binary(self, tmp_path):
        f = tmp_path / "mystery"
        f.write_bytes(b"\x00\x01\x02\x03")
        assert _is_text_file(f, "application/octet-stream") is False

    def test_no_extension_utf8_text(self, tmp_path):
        f = tmp_path / "script"
        f.write_text("#!/bin/bash\necho hello")
        assert _is_text_file(f, "application/octet-stream") is True

    def test_mime_text_type(self, tmp_path):
        f = tmp_path / "test.weird"
        f.write_text("hello")
        assert _is_text_file(f, "text/plain") is True

    def test_application_json_mime(self, tmp_path):
        f = tmp_path / "test.weird"
        f.write_text("{}")
        assert _is_text_file(f, "application/json") is True


# ---------------------------------------------------------------------------
# Route integration tests
# ---------------------------------------------------------------------------


class TestFileRoutes:
    @pytest.fixture(autouse=True)
    def _disable_auth(self, monkeypatch):
        import main as app_mod
        import auth
        monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
        auth.configure("")

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        import main as app_mod
        return TestClient(app_mod.app)

    def test_files_page_returns_html(self, client):
        resp = client.get("/files")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_files_path_page_returns_html(self, client):
        resp = client.get("/files/tmp")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_api_browse_root(self, client):
        resp = client.get("/api/files/browse?path=/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "directory"
        assert isinstance(data["entries"], list)

    def test_api_browse_tmp(self, client):
        resp = client.get("/api/files/browse?path=/tmp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "directory"

    def test_api_browse_blocked_path(self, client):
        resp = client.get("/api/files/browse?path=/proc")
        assert resp.status_code == 403

    def test_api_browse_nonexistent(self, client):
        resp = client.get("/api/files/browse?path=/nonexistent_path_xyz123")
        assert resp.status_code == 404

    def test_api_browse_file(self, client, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        resp = client.get(f"/api/files/browse?path={f}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "file"
        assert data["is_text"] is True

    def test_api_content(self, client, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("print('hello')")
        resp = client.get(f"/api/files/content?path={f}")
        assert resp.status_code == 200
        data = resp.json()
        assert "print" in data["content"]
        assert data["truncated"] is False

    def test_api_content_blocked(self, client):
        resp = client.get("/api/files/content?path=/proc/self/status")
        assert resp.status_code == 403

    def test_api_content_directory(self, client):
        resp = client.get("/api/files/content?path=/tmp")
        assert resp.status_code == 400

    def test_api_raw_serves_file(self, client, tmp_path):
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        resp = client.get(f"/api/files/raw?path={f}")
        assert resp.status_code == 200
        assert resp.content == b"\x89PNG\r\n\x1a\n"

    def test_api_raw_blocked(self, client):
        resp = client.get("/api/files/raw?path=/dev/null")
        assert resp.status_code == 403

    def test_api_raw_nonexistent(self, client):
        resp = client.get("/api/files/raw?path=/tmp/nonexistent_xyz123")
        assert resp.status_code == 404

    def test_api_raw_directory(self, client):
        resp = client.get("/api/files/raw?path=/tmp")
        assert resp.status_code == 400
