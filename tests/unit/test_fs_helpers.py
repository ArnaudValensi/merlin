"""Tests for files/fs_helpers.py — path validation, directory listing, file reading."""

import pytest

from files.fs_helpers import (
    IMAGE_EXTENSIONS,
    MODEL_3D_EXTENSIONS,
    TEXT_MAX_BYTES,
    _check_not_shallow,
    create_item,
    delete_item,
    get_file_info,
    list_directory,
    read_text_file,
    rename_item,
    sanitize_filename,
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

    def test_has_git_flag_for_git_repos(self, tmp_path):
        """Directories with .git should have has_git=True."""
        git_repo = tmp_path / "my-repo"
        git_repo.mkdir()
        (git_repo / ".git").mkdir()
        non_repo = tmp_path / "not-repo"
        non_repo.mkdir()

        result = list_directory(tmp_path)
        entries = {e["name"]: e for e in result["entries"]}
        assert entries["my-repo"]["has_git"] is True
        assert entries["not-repo"]["has_git"] is False

    def test_has_git_not_on_files(self, tmp_path):
        """Files should not have has_git key."""
        (tmp_path / "file.txt").write_text("hello")
        result = list_directory(tmp_path)
        assert "has_git" not in result["entries"][0]


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

    def test_stl_file_is_3d_model(self, tmp_path):
        f = tmp_path / "bracket.stl"
        f.write_bytes(b"solid bracket\n")
        info = get_file_info(f)
        assert info["is_3d_model"] is True
        assert info["is_image"] is False
        assert info["is_audio"] is False
        assert info["is_video"] is False

    def test_obj_file_is_3d_model(self, tmp_path):
        f = tmp_path / "model.obj"
        f.write_text("v 0 0 0\n")
        info = get_file_info(f)
        assert info["is_3d_model"] is True

    def test_non_3d_file_is_not_3d_model(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"\x89PNG\r\n")
        info = get_file_info(f)
        assert info["is_3d_model"] is False

    def test_all_model_3d_extensions_detected(self, tmp_path):
        for ext in MODEL_3D_EXTENSIONS:
            f = tmp_path / f"test{ext}"
            f.write_bytes(b"\x00")
            info = get_file_info(f)
            assert info["is_3d_model"] is True, (
                f"Expected {ext} to be detected as 3D model"
            )


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
        monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
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

    def test_api_browse_repo_root_for_git_dir(self, client, tmp_path):
        """Browse API should return repo_root when inside a git repo."""
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        sub = repo / "src"
        sub.mkdir()
        resp = client.get(f"/api/files/browse?path={sub}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_root"] == str(repo)

    def test_api_browse_repo_root_none_outside_git(self, client, tmp_path):
        """Browse API should return repo_root=null outside git repos."""
        resp = client.get(f"/api/files/browse?path={tmp_path}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_root"] is None


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    def test_simple_name(self):
        assert sanitize_filename("photo.jpg") == "photo.jpg"

    def test_strips_directory_path(self):
        assert sanitize_filename("foo/bar/photo.jpg") == "photo.jpg"

    def test_strips_backslash_path(self):
        assert sanitize_filename("C:\\Users\\foo\\photo.jpg") == "photo.jpg"

    def test_path_traversal(self):
        assert sanitize_filename("../../etc/passwd") == "passwd"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Invalid filename"):
            sanitize_filename("")

    def test_dot_only_raises(self):
        with pytest.raises(ValueError, match="Invalid filename"):
            sanitize_filename(".")

    def test_dotdot_raises(self):
        with pytest.raises(ValueError, match="Invalid filename"):
            sanitize_filename("..")

    def test_spaces_in_name(self):
        assert sanitize_filename("my photo (1).jpg") == "my photo (1).jpg"

    def test_unicode_preserved(self):
        assert sanitize_filename("café.txt") == "café.txt"

    def test_whitespace_stripped(self):
        assert sanitize_filename("  photo.jpg  ") == "photo.jpg"

    def test_null_byte_rejected(self):
        with pytest.raises(ValueError, match="Invalid filename"):
            sanitize_filename("evil\x00.txt")

    def test_null_byte_only_rejected(self):
        with pytest.raises(ValueError, match="Invalid filename"):
            sanitize_filename("\x00")

    def test_dot_slash_prefix_stripped(self):
        """./myfile.txt should become myfile.txt (path component stripped)."""
        assert sanitize_filename("./myfile.txt") == "myfile.txt"

    def test_dot_slash_dot_hidden(self):
        """./.hidden should become .hidden."""
        assert sanitize_filename("./.hidden") == ".hidden"


# ---------------------------------------------------------------------------
# Upload endpoint
# ---------------------------------------------------------------------------


class TestUploadRoutes:
    @pytest.fixture(autouse=True)
    def _disable_auth(self, monkeypatch):
        import main as app_mod
        import auth

        monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
        monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
        auth.configure("")

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        import main as app_mod

        return TestClient(app_mod.app)

    def test_upload_single_file(self, client, tmp_path):
        resp = client.post(
            "/api/files/upload",
            data={"directory": str(tmp_path)},
            files=[("files", ("test.txt", b"hello world", "text/plain"))],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["uploaded"]) == 1
        assert data["uploaded"][0]["name"] == "test.txt"
        assert (tmp_path / "test.txt").read_text() == "hello world"

    def test_upload_multiple_files(self, client, tmp_path):
        resp = client.post(
            "/api/files/upload",
            data={"directory": str(tmp_path)},
            files=[
                ("files", ("a.txt", b"aaa", "text/plain")),
                ("files", ("b.txt", b"bbb", "text/plain")),
            ],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["uploaded"]) == 2
        assert (tmp_path / "a.txt").read_text() == "aaa"
        assert (tmp_path / "b.txt").read_text() == "bbb"

    def test_upload_overwrite_existing(self, client, tmp_path):
        (tmp_path / "test.txt").write_text("old content")
        resp = client.post(
            "/api/files/upload",
            data={"directory": str(tmp_path)},
            files=[("files", ("test.txt", b"new content", "text/plain"))],
        )
        assert resp.status_code == 200
        assert (tmp_path / "test.txt").read_text() == "new content"

    def test_upload_preserves_binary_content(self, client, tmp_path):
        binary_data = bytes(range(256)) * 100
        resp = client.post(
            "/api/files/upload",
            data={"directory": str(tmp_path)},
            files=[("files", ("data.bin", binary_data, "application/octet-stream"))],
        )
        assert resp.status_code == 200
        assert (tmp_path / "data.bin").read_bytes() == binary_data

    def test_upload_to_blocked_path(self, client):
        resp = client.post(
            "/api/files/upload",
            data={"directory": "/proc"},
            files=[("files", ("test.txt", b"x", "text/plain"))],
        )
        assert resp.status_code == 403

    def test_upload_to_nonexistent_dir(self, client):
        resp = client.post(
            "/api/files/upload",
            data={"directory": "/nonexistent_xyz_123"},
            files=[("files", ("test.txt", b"x", "text/plain"))],
        )
        assert resp.status_code == 404

    def test_upload_to_file_not_dir(self, client, tmp_path):
        f = tmp_path / "afile.txt"
        f.write_text("hi")
        resp = client.post(
            "/api/files/upload",
            data={"directory": str(f)},
            files=[("files", ("test.txt", b"x", "text/plain"))],
        )
        assert resp.status_code == 400

    def test_upload_path_traversal_filename(self, client, tmp_path):
        resp = client.post(
            "/api/files/upload",
            data={"directory": str(tmp_path)},
            files=[("files", ("../../etc/passwd", b"hacked", "text/plain"))],
        )
        assert resp.status_code == 200
        # File should be written as "passwd" in the target directory
        assert (tmp_path / "passwd").read_bytes() == b"hacked"
        assert not (tmp_path / ".." / ".." / "etc" / "passwd").exists()

    def test_upload_returns_size(self, client, tmp_path):
        resp = client.post(
            "/api/files/upload",
            data={"directory": str(tmp_path)},
            files=[("files", ("test.txt", b"12345", "text/plain"))],
        )
        assert resp.status_code == 200
        assert resp.json()["uploaded"][0]["size"] == 5

    def test_upload_special_characters_filename(self, client, tmp_path):
        resp = client.post(
            "/api/files/upload",
            data={"directory": str(tmp_path)},
            files=[("files", ("my file (1).jpg", b"\x89PNG", "image/jpeg"))],
        )
        assert resp.status_code == 200
        assert (tmp_path / "my file (1).jpg").exists()


# ---------------------------------------------------------------------------
# create_item
# ---------------------------------------------------------------------------


class TestCreateItem:
    def test_create_file(self, tmp_path):
        result = create_item(tmp_path, "test.txt", "file")
        assert result.exists()
        assert result.is_file()
        assert result.name == "test.txt"

    def test_create_dir(self, tmp_path):
        result = create_item(tmp_path, "subdir", "dir")
        assert result.exists()
        assert result.is_dir()
        assert result.name == "subdir"

    def test_create_file_conflict(self, tmp_path):
        (tmp_path / "exists.txt").write_text("hi")
        with pytest.raises(FileExistsError, match="Already exists"):
            create_item(tmp_path, "exists.txt", "file")

    def test_create_dir_conflict(self, tmp_path):
        (tmp_path / "exists").mkdir()
        with pytest.raises(FileExistsError, match="Already exists"):
            create_item(tmp_path, "exists", "dir")

    def test_create_invalid_type(self, tmp_path):
        with pytest.raises(ValueError, match="type must be"):
            create_item(tmp_path, "test.txt", "symlink")

    def test_create_sanitizes_name(self, tmp_path):
        result = create_item(tmp_path, "../../etc/evil.txt", "file")
        assert result.name == "evil.txt"
        assert result.parent == tmp_path

    def test_create_empty_name(self, tmp_path):
        with pytest.raises(ValueError, match="Invalid filename"):
            create_item(tmp_path, "", "file")

    def test_create_dotdot_name(self, tmp_path):
        with pytest.raises(ValueError, match="Invalid filename"):
            create_item(tmp_path, "..", "dir")

    def test_create_null_byte_name(self, tmp_path):
        with pytest.raises(ValueError, match="Invalid filename"):
            create_item(tmp_path, "evil\x00.txt", "file")

    def test_create_dot_slash_stripped(self, tmp_path):
        """./myfile becomes myfile (path component stripped)."""
        result = create_item(tmp_path, "./myfile.txt", "file")
        assert result.name == "myfile.txt"
        assert result.parent == tmp_path


# ---------------------------------------------------------------------------
# rename_item
# ---------------------------------------------------------------------------


class TestRenameItem:
    def test_rename_file(self, tmp_path):
        f = tmp_path / "old.txt"
        f.write_text("content")
        result = rename_item(f, "new.txt")
        assert result.exists()
        assert result.name == "new.txt"
        assert not f.exists()

    def test_rename_dir(self, tmp_path):
        d = tmp_path / "olddir"
        d.mkdir()
        (d / "child.txt").write_text("hi")
        result = rename_item(d, "newdir")
        assert result.is_dir()
        assert (result / "child.txt").read_text() == "hi"
        assert not d.exists()

    def test_rename_conflict(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        with pytest.raises(FileExistsError, match="Already exists"):
            rename_item(tmp_path / "a.txt", "b.txt")

    def test_rename_nonexistent(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            rename_item(tmp_path / "nope.txt", "new.txt")

    def test_rename_sanitizes_name(self, tmp_path):
        f = tmp_path / "old.txt"
        f.write_text("data")
        result = rename_item(f, "../../etc/evil.txt")
        assert result.name == "evil.txt"
        assert result.parent == tmp_path

    def test_rename_preserves_content(self, tmp_path):
        f = tmp_path / "old.txt"
        f.write_text("important data")
        result = rename_item(f, "new.txt")
        assert result.read_text() == "important data"

    def test_rename_shallow_path_blocked(self):
        """Cannot rename top-level directories like /home."""
        from pathlib import Path

        with pytest.raises(PermissionError, match="Cannot modify system path"):
            rename_item(Path("/tmp"), "newtmp")

    def test_rename_null_byte_in_name(self, tmp_path):
        f = tmp_path / "old.txt"
        f.write_text("data")
        with pytest.raises(ValueError, match="Invalid filename"):
            rename_item(f, "new\x00.txt")


# ---------------------------------------------------------------------------
# delete_item
# ---------------------------------------------------------------------------


class TestDeleteItem:
    def test_delete_file(self, tmp_path):
        f = tmp_path / "doomed.txt"
        f.write_text("bye")
        delete_item(f)
        assert not f.exists()

    def test_delete_empty_dir(self, tmp_path):
        d = tmp_path / "emptydir"
        d.mkdir()
        delete_item(d)
        assert not d.exists()

    def test_delete_dir_with_contents(self, tmp_path):
        d = tmp_path / "full"
        d.mkdir()
        (d / "child.txt").write_text("data")
        (d / "sub").mkdir()
        (d / "sub" / "deep.txt").write_text("deep")
        delete_item(d)
        assert not d.exists()

    def test_delete_nonexistent(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            delete_item(tmp_path / "nope.txt")

    def test_delete_symlink(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("real")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        delete_item(link)
        assert not link.exists()
        assert target.exists()  # target preserved

    def test_delete_root_blocked(self):
        """Cannot delete the filesystem root."""
        from pathlib import Path

        with pytest.raises(PermissionError, match="Cannot modify system path"):
            delete_item(Path("/"))

    def test_delete_top_level_dir_blocked(self):
        """Cannot delete top-level directories like /home, /tmp, /usr."""
        from pathlib import Path

        with pytest.raises(PermissionError, match="Cannot modify system path"):
            delete_item(Path("/home"))

    def test_delete_deep_path_allowed(self, tmp_path):
        """Deep paths (depth >= 2) are allowed."""
        f = tmp_path / "safe.txt"
        f.write_text("ok")
        delete_item(f)
        assert not f.exists()


# ---------------------------------------------------------------------------
# _check_not_shallow
# ---------------------------------------------------------------------------


class TestCheckNotShallow:
    def test_root_blocked(self):
        from pathlib import Path

        with pytest.raises(PermissionError, match="Cannot modify system path"):
            _check_not_shallow(Path("/"))

    def test_top_level_blocked(self):
        from pathlib import Path

        with pytest.raises(PermissionError, match="Cannot modify system path"):
            _check_not_shallow(Path("/home"))

    def test_depth_2_allowed(self):
        from pathlib import Path

        _check_not_shallow(Path("/home/user"))  # should not raise

    def test_deep_path_allowed(self, tmp_path):
        _check_not_shallow(tmp_path / "subdir" / "file.txt")  # should not raise


# ---------------------------------------------------------------------------
# Create/Rename/Delete route tests
# ---------------------------------------------------------------------------


class TestCreateRoutes:
    @pytest.fixture(autouse=True)
    def _disable_auth(self, monkeypatch):
        import main as app_mod
        import auth

        monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
        monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
        auth.configure("")

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        import main as app_mod

        return TestClient(app_mod.app)

    def test_create_file_endpoint(self, client, tmp_path):
        resp = client.post(
            "/api/files/create",
            json={"path": str(tmp_path), "name": "new.txt", "type": "file"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "new.txt"
        assert (tmp_path / "new.txt").exists()

    def test_create_dir_endpoint(self, client, tmp_path):
        resp = client.post(
            "/api/files/create",
            json={"path": str(tmp_path), "name": "newdir", "type": "dir"},
        )
        assert resp.status_code == 200
        assert (tmp_path / "newdir").is_dir()

    def test_create_conflict_returns_409(self, client, tmp_path):
        (tmp_path / "exists.txt").write_text("hi")
        resp = client.post(
            "/api/files/create",
            json={"path": str(tmp_path), "name": "exists.txt", "type": "file"},
        )
        assert resp.status_code == 409

    def test_create_in_blocked_path(self, client):
        resp = client.post(
            "/api/files/create", json={"path": "/proc", "name": "test", "type": "file"}
        )
        assert resp.status_code == 403

    def test_create_in_nonexistent_dir(self, client):
        resp = client.post(
            "/api/files/create",
            json={"path": "/nonexistent_xyz_123", "name": "test", "type": "file"},
        )
        assert resp.status_code == 404

    def test_create_invalid_type(self, client, tmp_path):
        resp = client.post(
            "/api/files/create",
            json={"path": str(tmp_path), "name": "test", "type": "symlink"},
        )
        assert resp.status_code == 422  # Pydantic rejects invalid Literal

    def test_create_null_byte_name(self, client, tmp_path):
        resp = client.post(
            "/api/files/create",
            json={"path": str(tmp_path), "name": "evil\x00.txt", "type": "file"},
        )
        assert resp.status_code == 400


class TestRenameRoutes:
    @pytest.fixture(autouse=True)
    def _disable_auth(self, monkeypatch):
        import main as app_mod
        import auth

        monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
        monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
        auth.configure("")

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        import main as app_mod

        return TestClient(app_mod.app)

    def test_rename_file_endpoint(self, client, tmp_path):
        f = tmp_path / "old.txt"
        f.write_text("data")
        resp = client.post(
            "/api/files/rename", json={"path": str(f), "new_name": "new.txt"}
        )
        assert resp.status_code == 200
        assert resp.json()["new_name"] == "new.txt"
        assert (tmp_path / "new.txt").exists()
        assert not f.exists()

    def test_rename_conflict_returns_409(self, client, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        resp = client.post(
            "/api/files/rename",
            json={"path": str(tmp_path / "a.txt"), "new_name": "b.txt"},
        )
        assert resp.status_code == 409

    def test_rename_blocked_path(self, client):
        resp = client.post(
            "/api/files/rename", json={"path": "/proc/self", "new_name": "nope"}
        )
        assert resp.status_code == 403

    def test_rename_nonexistent_returns_404(self, client, tmp_path):
        resp = client.post(
            "/api/files/rename",
            json={"path": str(tmp_path / "nope.txt"), "new_name": "new.txt"},
        )
        assert resp.status_code == 404

    def test_rename_shallow_path_blocked(self, client):
        resp = client.post(
            "/api/files/rename", json={"path": "/tmp", "new_name": "newtmp"}
        )
        assert resp.status_code == 403


class TestDeleteRoutes:
    @pytest.fixture(autouse=True)
    def _disable_auth(self, monkeypatch):
        import main as app_mod
        import auth

        monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
        monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
        auth.configure("")

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        import main as app_mod

        return TestClient(app_mod.app)

    def test_delete_file_endpoint(self, client, tmp_path):
        f = tmp_path / "doomed.txt"
        f.write_text("bye")
        resp = client.post("/api/files/delete", json={"path": str(f)})
        assert resp.status_code == 200
        assert not f.exists()

    def test_delete_dir_endpoint(self, client, tmp_path):
        d = tmp_path / "doomdir"
        d.mkdir()
        (d / "child.txt").write_text("data")
        resp = client.post("/api/files/delete", json={"path": str(d)})
        assert resp.status_code == 200
        assert not d.exists()

    def test_delete_blocked_path(self, client):
        resp = client.post("/api/files/delete", json={"path": "/proc/self"})
        assert resp.status_code == 403

    def test_delete_nonexistent_returns_404(self, client, tmp_path):
        resp = client.post(
            "/api/files/delete", json={"path": str(tmp_path / "nope.txt")}
        )
        assert resp.status_code == 404

    def test_delete_root_blocked(self, client):
        resp = client.post("/api/files/delete", json={"path": "/"})
        assert resp.status_code == 403

    def test_delete_top_level_blocked(self, client):
        resp = client.post("/api/files/delete", json={"path": "/home"})
        assert resp.status_code == 403
