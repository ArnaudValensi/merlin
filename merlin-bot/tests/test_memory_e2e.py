"""End-to-end tests for the memory system.

Tests the full flow across all memory components:
remember.py, kb_add.py, memory_search.py working together.
"""

import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def memory_env(tmp_path, monkeypatch):
    """Set up a complete temporary memory environment for all scripts."""
    import memory_search
    import kb_add
    import remember

    kb_dir = tmp_path / "kb"
    logs_dir = tmp_path / "logs"
    kb_dir.mkdir()
    logs_dir.mkdir()

    # Create _index.md
    (kb_dir / "_index.md").write_text(textwrap.dedent("""\
        ---
        title: Knowledge Base Index
        created: 2026-01-01
        tags: [index, meta]
        summary: Entry point
        ---

        # Knowledge Base
    """))

    # Create user.md
    user_md = tmp_path / "user.md"
    user_md.write_text(textwrap.dedent("""\
        # User Memory

        Facts about the user that Merlin should always remember.

        ## Identity

        - Name: (to be filled in)

        ## Preferences

        - Communication style: (to be filled in)

        ## Context

        - Current projects: (to be filled in)

        ## Notes

        (Add important facts here as they come up in conversation)
    """))

    # Create a log file
    (logs_dir / "2026-02-05.md").write_text(textwrap.dedent("""\
        # Daily Log — 2026-02-05

        ## 10:00 — Session notes

        - Discussed Docker container networking
        - User interested in Zettelkasten method
    """))

    # Patch all modules
    monkeypatch.setattr(memory_search, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(memory_search, "KB_DIR", kb_dir)
    monkeypatch.setattr(memory_search, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(kb_add, "KB_DIR", kb_dir)
    monkeypatch.setattr(remember, "USER_MD", user_md)

    return tmp_path


class TestFullFlow:
    """End-to-end tests exercising the full memory system."""

    def test_kb_add_then_search(self, memory_env, capsys):
        """Create a KB entry, then verify search finds it."""
        import argparse
        from kb_add import cmd_add
        from memory_search import cmd_kb

        # Add a KB entry
        cmd_add(argparse.Namespace(
            title="Docker Networking",
            tags="devops, docker, networking",
            summary="How container networking works",
            content="Docker uses bridge networks by default. Custom networks isolate services.",
            filename=None, dry_run=False, force=False,
        ))
        capsys.readouterr()  # clear

        # Search should find it by keyword
        cmd_kb(argparse.Namespace(keyword="bridge", tag=None, discord=False))
        output = capsys.readouterr().out
        assert "Docker Networking" in output

        # Search should find it by tag
        cmd_kb(argparse.Namespace(keyword=None, tag="networking", discord=False))
        output = capsys.readouterr().out
        assert "Docker Networking" in output

    def test_kb_add_links_related(self, memory_env, capsys):
        """Create two related KB entries and verify they link to each other."""
        import argparse
        from kb_add import cmd_add, parse_frontmatter

        kb_dir = memory_env / "kb"

        # Create first entry
        cmd_add(argparse.Namespace(
            title="Python Testing",
            tags="python, testing",
            summary="Python test frameworks",
            content="Use pytest for testing. Fixtures for setup. Monkeypatch for mocking.",
            filename=None, dry_run=False, force=False,
        ))

        # Create second entry with overlapping tags
        cmd_add(argparse.Namespace(
            title="Pytest Fixtures",
            tags="python, testing, pytest",
            summary="How to use pytest fixtures",
            content="Fixtures provide setup and teardown. Use tmp_path for temp files. Monkeypatch for patching.",
            filename=None, dry_run=False, force=False,
        ))

        # Verify bidirectional links
        fm1 = parse_frontmatter(kb_dir / "python-testing.md")
        fm2 = parse_frontmatter(kb_dir / "pytest-fixtures.md")

        assert "pytest-fixtures.md" in fm1.get("related", "")
        assert "python-testing.md" in fm2.get("related", "")

    def test_remember_then_list(self, memory_env, capsys):
        """Remember facts, then verify they show up in list."""
        from remember import add_fact, list_facts

        add_fact("Name: Alex", section="identity")
        add_fact("Prefers dark mode", section="preferences")
        add_fact("Working on Merlin", section="context")
        add_fact("Likes flat directory structures")

        output = list_facts()
        assert "Name: Alex" in output
        assert "Prefers dark mode" in output
        assert "Working on Merlin" in output
        assert "flat directory" in output

    def test_log_search_finds_entries(self, memory_env, capsys):
        """Search logs for content."""
        import argparse
        from memory_search import cmd_log

        cmd_log(argparse.Namespace(
            keyword="Docker", date_from=None, date_to=None,
            last=None, discord=False,
        ))
        output = capsys.readouterr().out
        assert "2026-02-05.md" in output

    def test_tags_reflect_kb_entries(self, memory_env, capsys):
        """Tag index should reflect all KB entries."""
        import argparse
        from kb_add import cmd_add
        from memory_search import cmd_tags

        cmd_add(argparse.Namespace(
            title="Music Gear", tags="music, shopping",
            summary="Audio equipment notes", content="Looking for a receiver.",
            filename=None, dry_run=False, force=False,
        ))
        cmd_add(argparse.Namespace(
            title="Travel Plans", tags="travel, personal",
            summary="Upcoming trips", content="Planning a trip.",
            filename=None, dry_run=False, force=False,
        ))
        capsys.readouterr()  # clear

        cmd_tags(argparse.Namespace())
        output = capsys.readouterr().out
        assert "music" in output
        assert "shopping" in output
        assert "travel" in output
        assert "personal" in output

    def test_duplicate_detection_across_flow(self, memory_env, capsys):
        """Creating a duplicate KB entry should warn."""
        import argparse
        from kb_add import cmd_add

        cmd_add(argparse.Namespace(
            title="Unique Topic", tags="test",
            summary="A topic", content="Some content.",
            filename=None, dry_run=False, force=False,
        ))

        # Attempting to create same title should fail
        with pytest.raises(SystemExit):
            cmd_add(argparse.Namespace(
                title="Unique Topic", tags="test",
                summary="Duplicate", content="Different content.",
                filename=None, dry_run=False, force=False,
            ))
