"""Tests for memory_search.py — memory search tools."""

import subprocess
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def memory_dir(tmp_path, monkeypatch):
    """Create a temporary memory directory structure with test data."""
    import memory_search

    kb_dir = tmp_path / "kb"
    logs_dir = tmp_path / "logs"
    kb_dir.mkdir()
    logs_dir.mkdir()

    monkeypatch.setattr(memory_search, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(memory_search, "KB_DIR", kb_dir)
    monkeypatch.setattr(memory_search, "LOGS_DIR", logs_dir)

    return tmp_path


@pytest.fixture
def kb_with_entries(memory_dir):
    """Populate KB with test entries."""
    kb_dir = memory_dir / "kb"

    (kb_dir / "_index.md").write_text(textwrap.dedent("""\
        ---
        title: Knowledge Base Index
        created: 2026-01-01
        tags: [index, meta]
        summary: Entry point
        ---

        # Knowledge Base
    """))

    (kb_dir / "docker-setup.md").write_text(textwrap.dedent("""\
        ---
        title: Docker Setup
        created: 2026-01-15
        tags: [devops, docker]
        summary: How to set up Docker containers
        ---

        # Docker Setup

        Use docker-compose for local development.
        Mount volumes for persistent data.
    """))

    (kb_dir / "tech-gear.md").write_text(textwrap.dedent("""\
        ---
        title: Tech Gear
        created: 2026-01-20
        tags: [tech, personal]
        summary: Notes on tech equipment
        ---

        # Tech Gear

        Looking for a good mechanical keyboard.
        Budget is under 200 USD.
    """))

    return kb_dir


@pytest.fixture
def logs_with_entries(memory_dir):
    """Populate logs with test entries."""
    logs_dir = memory_dir / "logs"

    (logs_dir / "2026-01-28.md").write_text(textwrap.dedent("""\
        # Daily Log — 2026-01-28

        ## 10:00 — Pre-compaction memories

        - User prefers flat directory structures
        - Working on Discord bot project
    """))

    (logs_dir / "2026-02-01.md").write_text(textwrap.dedent("""\
        # Daily Log — 2026-02-01

        ## 14:00 — Pre-compaction memories

        - Discussed cron job scheduling
        - User wants silent report mode for monitoring

        ## 16:00 — Pre-compaction memories

        - Debugging webhook integration
    """))

    (logs_dir / "2026-02-05.md").write_text(textwrap.dedent("""\
        # Daily Log — 2026-02-05

        ## 18:10 — Pre-compaction memories

        - User prefers flat directory structures
        - User likes standard markdown links
    """))

    return logs_dir


class TestParseFrontmatter:
    """Tests for YAML frontmatter parsing."""

    def test_full_frontmatter(self, kb_with_entries):
        from memory_search import _parse_frontmatter

        fm = _parse_frontmatter(kb_with_entries / "docker-setup.md")
        assert fm["title"] == "Docker Setup"
        assert fm["created"] == "2026-01-15"
        assert "docker" in fm["tags"]
        assert fm["summary"] == "How to set up Docker containers"

    def test_no_frontmatter(self, tmp_path):
        from memory_search import _parse_frontmatter

        f = tmp_path / "plain.md"
        f.write_text("# Just a heading\n\nNo frontmatter here.\n")
        fm = _parse_frontmatter(f)
        assert fm == {}

    def test_empty_file(self, tmp_path):
        from memory_search import _parse_frontmatter

        f = tmp_path / "empty.md"
        f.write_text("")
        fm = _parse_frontmatter(f)
        assert fm == {}


class TestKbSearch:
    """Tests for knowledge base search."""

    def test_kb_list(self, kb_with_entries, capsys):
        from memory_search import cmd_kb
        import argparse

        args = argparse.Namespace(keyword=None, tag=None, discord=False)
        cmd_kb(args)
        output = capsys.readouterr().out
        assert "2 entries" in output
        assert "Docker Setup" in output
        assert "Tech Gear" in output
        # _index.md should be excluded from list
        assert "Knowledge Base Index" not in output

    def test_kb_tag_search(self, kb_with_entries, capsys):
        from memory_search import cmd_kb
        import argparse

        args = argparse.Namespace(keyword=None, tag="tech", discord=False)
        cmd_kb(args)
        output = capsys.readouterr().out
        assert "Tech Gear" in output
        assert "Docker Setup" not in output

    def test_kb_tag_no_results(self, kb_with_entries, capsys):
        from memory_search import cmd_kb
        import argparse

        args = argparse.Namespace(keyword=None, tag="nonexistent", discord=False)
        cmd_kb(args)
        output = capsys.readouterr().out
        assert "no KB entries" in output

    def test_kb_keyword_search(self, kb_with_entries, capsys):
        from memory_search import cmd_kb
        import argparse

        args = argparse.Namespace(keyword="keyboard", tag=None, discord=False)
        cmd_kb(args)
        output = capsys.readouterr().out
        assert "Tech Gear" in output
        assert "keyboard" in output

    def test_kb_keyword_no_results(self, kb_with_entries, capsys):
        from memory_search import cmd_kb
        import argparse

        args = argparse.Namespace(keyword="nonexistent_xyz", tag=None, discord=False)
        cmd_kb(args)
        output = capsys.readouterr().out
        assert "no KB matches" in output

    def test_kb_empty(self, memory_dir, capsys):
        from memory_search import cmd_kb
        import argparse

        args = argparse.Namespace(keyword=None, tag=None, discord=False)
        cmd_kb(args)
        output = capsys.readouterr().out
        assert "no KB entries" in output


class TestLogSearch:
    """Tests for daily log search."""

    def test_log_list_all(self, logs_with_entries, capsys):
        from memory_search import cmd_log
        import argparse

        args = argparse.Namespace(keyword=None, date_from=None, date_to=None,
                                  last=None, discord=False)
        cmd_log(args)
        output = capsys.readouterr().out
        assert "3 files" in output
        assert "2026-01-28.md" in output
        assert "2026-02-05.md" in output

    def test_log_list_date_range(self, logs_with_entries, capsys):
        from memory_search import cmd_log
        import argparse

        args = argparse.Namespace(keyword=None, date_from="2026-02-01",
                                  date_to="2026-02-28", last=None, discord=False)
        cmd_log(args)
        output = capsys.readouterr().out
        assert "2 files" in output
        assert "2026-01-28.md" not in output
        assert "2026-02-01.md" in output

    def test_log_keyword_search(self, logs_with_entries, capsys):
        from memory_search import cmd_log
        import argparse

        args = argparse.Namespace(keyword="cron", date_from=None, date_to=None,
                                  last=None, discord=False)
        cmd_log(args)
        output = capsys.readouterr().out
        assert "2026-02-01.md" in output
        assert "cron" in output.lower()

    def test_log_keyword_no_results(self, logs_with_entries, capsys):
        from memory_search import cmd_log
        import argparse

        args = argparse.Namespace(keyword="nonexistent_xyz", date_from=None,
                                  date_to=None, last=None, discord=False)
        cmd_log(args)
        output = capsys.readouterr().out
        assert "no log matches" in output

    def test_log_last_n_days(self, logs_with_entries, capsys):
        from memory_search import _resolve_date_range
        import argparse

        args = argparse.Namespace(date_from=None, date_to=None, last=7)
        date_from, date_to = _resolve_date_range(args)
        assert date_from is not None
        assert date_to is not None

    def test_log_empty_dir(self, memory_dir, capsys):
        from memory_search import cmd_log
        import argparse

        args = argparse.Namespace(keyword=None, date_from=None, date_to=None,
                                  last=None, discord=False)
        cmd_log(args)
        output = capsys.readouterr().out
        assert "no log files" in output


class TestHelpers:
    """Tests for helper functions."""

    def test_get_log_files_no_filter(self, logs_with_entries):
        from memory_search import _get_log_files

        files = _get_log_files(None, None)
        assert len(files) == 3

    def test_get_log_files_from_filter(self, logs_with_entries):
        from memory_search import _get_log_files

        files = _get_log_files("2026-02-01", None)
        assert len(files) == 2
        assert all("2026-01" not in f.name for f in files)

    def test_get_log_files_to_filter(self, logs_with_entries):
        from memory_search import _get_log_files

        files = _get_log_files(None, "2026-02-01")
        assert len(files) == 2
        assert all("2026-02-05" not in f.name for f in files)

    def test_format_kb_result(self, kb_with_entries):
        from memory_search import _format_kb_result

        result = _format_kb_result(kb_with_entries / "docker-setup.md")
        assert "Docker Setup" in result
        assert "docker-setup.md" in result
        assert "How to set up Docker containers" in result


class TestTagIndex:
    """Tests for tag index generation."""

    def test_lists_all_tags(self, kb_with_entries, capsys):
        from memory_search import cmd_tags
        import argparse

        args = argparse.Namespace()
        cmd_tags(args)
        output = capsys.readouterr().out
        assert "KB Tags" in output
        assert "devops" in output
        assert "tech" in output
        assert "personal" in output

    def test_tag_counts(self, kb_with_entries, capsys):
        from memory_search import cmd_tags
        import argparse

        args = argparse.Namespace()
        cmd_tags(args)
        output = capsys.readouterr().out
        # "devops" appears in docker-setup.md and music-gear has "music, personal"
        assert "devops" in output
        assert "docker-setup.md" in output

    def test_no_tags(self, memory_dir, capsys):
        from memory_search import cmd_tags
        import argparse

        args = argparse.Namespace()
        cmd_tags(args)
        output = capsys.readouterr().out
        assert "no tags found" in output
