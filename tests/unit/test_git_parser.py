"""Tests for commits/git_parser.py — log parsing, diff parsing, gutter computation."""

import pytest

from commits.git_parser import (
    _parse_log_output,
    _parse_shortstat,
    _parse_unified_diff,
    _compute_gutters,
    _validate_hash,
    _validate_file_path,
)


# ---------------------------------------------------------------------------
# Hash / path validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_short_hash(self):
        assert _validate_hash("abcd1234") == "abcd1234"

    def test_valid_full_hash(self):
        h = "a" * 40
        assert _validate_hash(h) == h

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError):
            _validate_hash("ABCD1234")

    def test_rejects_too_short(self):
        with pytest.raises(ValueError):
            _validate_hash("abc")

    def test_rejects_special_chars(self):
        with pytest.raises(ValueError):
            _validate_hash("abcd; rm -rf /")

    def test_valid_file_path(self):
        _validate_file_path("src/main.py")  # should not raise

    def test_rejects_traversal(self):
        with pytest.raises(ValueError):
            _validate_file_path("../../etc/passwd")

    def test_rejects_absolute(self):
        with pytest.raises(ValueError):
            _validate_file_path("/etc/passwd")


# ---------------------------------------------------------------------------
# Log parsing (task #5)
# ---------------------------------------------------------------------------


class TestParseLogOutput:
    def test_single_commit(self):
        h = "a" * 40
        output = (
            f"{h}|abc1234|John Doe|2026-02-20T10:00:00+00:00|Fix bug\n"
            "\n"
            " 3 files changed, 10 insertions(+), 2 deletions(-)\n"
        )
        commits = _parse_log_output(output)
        assert len(commits) == 1
        assert commits[0]["short"] == "abc1234"
        assert commits[0]["message"] == "Fix bug"
        assert commits[0]["author"] == "John Doe"
        assert commits[0]["files_changed"] == 3
        assert commits[0]["insertions"] == 10
        assert commits[0]["deletions"] == 2

    def test_multiple_commits(self):
        h1 = "a" * 40
        h2 = "b" * 40
        output = (
            f"{h1}|aaaa|Alice|2026-02-20T10:00:00+00:00|First commit\n"
            "\n"
            " 1 file changed, 5 insertions(+)\n"
            f"{h2}|bbbb|Bob|2026-02-19T09:00:00+00:00|Second commit\n"
            "\n"
            " 2 files changed, 3 insertions(+), 1 deletion(-)\n"
        )
        commits = _parse_log_output(output)
        assert len(commits) == 2
        assert commits[0]["hash"] == h1
        assert commits[1]["hash"] == h2
        assert commits[0]["insertions"] == 5
        assert commits[0]["deletions"] == 0
        assert commits[1]["files_changed"] == 2

    def test_empty_output(self):
        assert _parse_log_output("") == []
        assert _parse_log_output("\n\n") == []

    def test_special_chars_in_message(self):
        h = "c" * 40
        output = f'{h}|cccc|Dev|2026-02-20T10:00:00+00:00|Fix "quotes" & <angles>\n'
        commits = _parse_log_output(output)
        assert len(commits) == 1
        assert commits[0]["message"] == 'Fix "quotes" & <angles>'

    def test_pipe_in_message(self):
        """Messages with pipes should keep everything after 4th pipe as message."""
        h = "d" * 40
        output = f"{h}|dddd|Dev|2026-02-20T10:00:00+00:00|Fix bug | with pipe\n"
        commits = _parse_log_output(output)
        assert len(commits) == 1
        assert commits[0]["message"] == "Fix bug | with pipe"

    def test_commit_without_stats(self):
        """Commits with no stat line (e.g., empty commits) should still parse."""
        h = "e" * 40
        output = f"{h}|eeee|Dev|2026-02-20T10:00:00+00:00|Empty commit\n"
        commits = _parse_log_output(output)
        assert len(commits) == 1
        assert commits[0]["files_changed"] == 0
        assert commits[0]["insertions"] == 0
        assert commits[0]["deletions"] == 0


class TestParseShortstat:
    def test_full_stat(self):
        result = _parse_shortstat(" 3 files changed, 10 insertions(+), 2 deletions(-)")
        assert result == {"files_changed": 3, "insertions": 10, "deletions": 2}

    def test_insertions_only(self):
        result = _parse_shortstat(" 1 file changed, 5 insertions(+)")
        assert result == {"files_changed": 1, "insertions": 5, "deletions": 0}

    def test_deletions_only(self):
        result = _parse_shortstat(" 2 files changed, 3 deletions(-)")
        assert result == {"files_changed": 2, "insertions": 0, "deletions": 3}

    def test_non_stat_line(self):
        assert _parse_shortstat("some random text") is None


# ---------------------------------------------------------------------------
# Diff parsing (task #7)
# ---------------------------------------------------------------------------


class TestParseUnifiedDiff:
    def test_single_file_addition(self):
        diff = (
            "diff --git a/hello.py b/hello.py\n"
            "new file mode 100644\n"
            "index 0000000..abc1234\n"
            "--- /dev/null\n"
            "+++ b/hello.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+print('hello')\n"
            "+print('world')\n"
            "+print('!')\n"
        )
        files = _parse_unified_diff(diff, {"hello.py": "A"})
        assert len(files) == 1
        assert files[0]["path"] == "hello.py"
        assert files[0]["status"] == "A"
        assert len(files[0]["hunks"]) == 1
        assert len(files[0]["hunks"][0]["lines"]) == 3
        assert all(l["type"] == "add" for l in files[0]["hunks"][0]["lines"])

    def test_single_file_deletion(self):
        diff = (
            "diff --git a/old.py b/old.py\n"
            "deleted file mode 100644\n"
            "index abc1234..0000000\n"
            "--- a/old.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-line1\n"
            "-line2\n"
        )
        files = _parse_unified_diff(diff, {"old.py": "D"})
        assert len(files) == 1
        assert files[0]["status"] == "D"
        assert len(files[0]["hunks"][0]["lines"]) == 2
        assert all(l["type"] == "del" for l in files[0]["hunks"][0]["lines"])

    def test_modification_with_context(self):
        diff = (
            "diff --git a/main.py b/main.py\n"
            "index abc..def 100644\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1,5 +1,5 @@\n"
            " context line 1\n"
            " context line 2\n"
            "-old line\n"
            "+new line\n"
            " context line 3\n"
            " context line 4\n"
        )
        files = _parse_unified_diff(diff)
        assert len(files) == 1
        hunk = files[0]["hunks"][0]
        types = [l["type"] for l in hunk["lines"]]
        assert types == ["context", "context", "del", "add", "context", "context"]

    def test_multi_file_diff(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,2 @@\n"
            " existing\n"
            "+added\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,2 +1,1 @@\n"
            " keep\n"
            "-removed\n"
        )
        files = _parse_unified_diff(diff)
        assert len(files) == 2
        assert files[0]["path"] == "a.py"
        assert files[1]["path"] == "b.py"

    def test_multi_hunk_diff(self):
        diff = (
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1,3 +1,3 @@\n"
            " line1\n"
            "-old2\n"
            "+new2\n"
            " line3\n"
            "@@ -10,3 +10,3 @@\n"
            " line10\n"
            "-old11\n"
            "+new11\n"
            " line12\n"
        )
        files = _parse_unified_diff(diff)
        assert len(files) == 1
        assert len(files[0]["hunks"]) == 2

    def test_line_numbers(self):
        diff = (
            "diff --git a/test.py b/test.py\n"
            "--- a/test.py\n"
            "+++ b/test.py\n"
            "@@ -5,4 +5,4 @@\n"
            " ctx\n"
            "-old\n"
            "+new\n"
            " ctx2\n"
        )
        files = _parse_unified_diff(diff)
        lines = files[0]["hunks"][0]["lines"]

        # Context line at old:5, new:5
        assert lines[0]["old_no"] == 5
        assert lines[0]["new_no"] == 5

        # Deletion at old:6
        assert lines[1]["old_no"] == 6
        assert lines[1]["new_no"] is None

        # Addition at new:6
        assert lines[2]["old_no"] is None
        assert lines[2]["new_no"] == 6

    def test_empty_diff(self):
        assert _parse_unified_diff("") == []
        assert _parse_unified_diff("\n\n") == []

    def test_binary_file(self):
        diff = (
            "diff --git a/image.png b/image.png\n"
            "Binary files /dev/null and b/image.png differ\n"
        )
        files = _parse_unified_diff(diff)
        assert len(files) == 1
        assert files[0].get("binary") is True
        assert files[0]["hunks"] == []

    def test_rename(self):
        diff = (
            "diff --git a/old_name.py b/new_name.py\n"
            "similarity index 100%\n"
            "rename from old_name.py\n"
            "rename to new_name.py\n"
        )
        files = _parse_unified_diff(diff, {"new_name.py": "R"})
        assert len(files) == 1
        assert files[0]["path"] == "new_name.py"
        assert files[0]["status"] == "R"


# ---------------------------------------------------------------------------
# Gutter computation (task #9)
# ---------------------------------------------------------------------------


class TestComputeGutters:
    def test_no_diff_no_gutters(self):
        """File with no diff should have no gutter markers."""
        content = "line1\nline2\nline3\n"
        result = _compute_gutters("", content)
        assert len(result) == 3
        assert all(l["gutter"] is None for l in result)
        assert all(l["deleted_lines"] == [] for l in result)

    def test_pure_additions(self):
        """Added lines should get 'added' gutter."""
        content = "existing\nnew1\nnew2\nmore\n"
        diff = (
            "diff --git a/test b/test\n"
            "--- a/test\n"
            "+++ b/test\n"
            "@@ -1,1 +1,4 @@\n"
            " existing\n"
            "+new1\n"
            "+new2\n"
            "+more\n"
        )
        result = _compute_gutters(diff, content)
        assert result[0]["gutter"] is None   # existing
        assert result[1]["gutter"] == "added"  # new1
        assert result[2]["gutter"] == "added"  # new2
        assert result[3]["gutter"] == "added"  # more

    def test_pure_deletions(self):
        """Deleted lines should mark nearest line with 'deleted' and store deleted content."""
        content = "line1\nline3\n"
        diff = (
            "diff --git a/test b/test\n"
            "--- a/test\n"
            "+++ b/test\n"
            "@@ -1,3 +1,2 @@\n"
            " line1\n"
            "-line2\n"
            " line3\n"
        )
        result = _compute_gutters(diff, content)
        assert result[0]["gutter"] is None
        # Deletion is between line1 and line3, marks the position
        assert result[1]["gutter"] == "deleted"
        assert result[1]["deleted_lines"] == ["line2"]

    def test_modification_replacement(self):
        """Lines that are replaced (del+add) should get 'modified' gutter."""
        content = "line1\nnew_line2\nline3\n"
        diff = (
            "diff --git a/test b/test\n"
            "--- a/test\n"
            "+++ b/test\n"
            "@@ -1,3 +1,3 @@\n"
            " line1\n"
            "-old_line2\n"
            "+new_line2\n"
            " line3\n"
        )
        result = _compute_gutters(diff, content)
        assert result[0]["gutter"] is None
        assert result[1]["gutter"] == "modified"
        assert result[1]["deleted_lines"] == ["old_line2"]
        assert result[2]["gutter"] is None

    def test_multi_hunk_file(self):
        """Multiple hunks in one file should apply gutters correctly."""
        content = "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n"
        diff = (
            "diff --git a/test b/test\n"
            "--- a/test\n"
            "+++ b/test\n"
            "@@ -2,1 +2,1 @@\n"
            "-old_b\n"
            "+b\n"
            "@@ -8,1 +8,1 @@\n"
            "-old_h\n"
            "+h\n"
        )
        result = _compute_gutters(diff, content)
        assert result[1]["gutter"] == "modified"  # line 2
        assert result[7]["gutter"] == "modified"  # line 8
        # Other lines have no gutter
        for i in [0, 2, 3, 4, 5, 6, 8, 9]:
            assert result[i]["gutter"] is None

    def test_all_new_file(self):
        """A newly added file: all lines should be 'added'."""
        content = "first\nsecond\nthird\n"
        diff = (
            "diff --git a/test b/test\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/test\n"
            "@@ -0,0 +1,3 @@\n"
            "+first\n"
            "+second\n"
            "+third\n"
        )
        result = _compute_gutters(diff, content)
        assert all(l["gutter"] == "added" for l in result)

    def test_multi_line_deletion_stored(self):
        """Multiple deleted lines should all be stored in deleted_lines."""
        content = "keep\n"
        diff = (
            "diff --git a/test b/test\n"
            "--- a/test\n"
            "+++ b/test\n"
            "@@ -1,4 +1,1 @@\n"
            " keep\n"
            "-del1\n"
            "-del2\n"
            "-del3\n"
        )
        result = _compute_gutters(diff, content)
        # The deletion should be attached somewhere
        deleted = [l for l in result if l["deleted_lines"]]
        assert len(deleted) == 1
        assert deleted[0]["deleted_lines"] == ["del1", "del2", "del3"]

    def test_empty_file(self):
        """Empty file content should return empty list."""
        result = _compute_gutters("", "")
        assert result == []
