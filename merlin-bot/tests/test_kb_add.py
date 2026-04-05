"""Tests for kb_add.py — knowledge base entry creation with link discovery."""

import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def kb_dir(tmp_path, monkeypatch):
    """Create a temporary KB directory and patch kb_add to use it."""
    import kb_add

    kb = tmp_path / "kb"
    kb.mkdir()

    # Create _index.md
    (kb / "_index.md").write_text(textwrap.dedent("""\
        ---
        title: Knowledge Base Index
        created: 2026-01-01
        tags: [index, meta]
        summary: Entry point
        ---

        # Knowledge Base
    """))

    monkeypatch.setattr(kb_add, "KB_DIR", kb)
    return kb


@pytest.fixture
def kb_with_notes(kb_dir):
    """KB populated with several test notes."""
    (kb_dir / "docker-setup.md").write_text(textwrap.dedent("""\
        ---
        title: Docker Setup
        created: 2026-01-15
        tags: [devops, docker]
        related: []
        summary: How to set up Docker
        ---

        # Docker Setup

        Install Docker on Arch Linux using pacman.
        Configure docker-compose for local development.
    """))

    (kb_dir / "tech-gear.md").write_text(textwrap.dedent("""\
        ---
        title: Tech Gear
        created: 2026-01-20
        tags: [tech, personal]
        related: []
        summary: Notes on tech equipment
        ---

        # Tech Gear

        Looking for a good mechanical keyboard.
        Budget is under 200 USD.
    """))

    (kb_dir / "arch-linux.md").write_text(textwrap.dedent("""\
        ---
        title: Arch Linux Notes
        created: 2026-01-25
        tags: [linux, devops]
        related: [docker-setup.md]
        summary: Arch Linux tips and tricks
        ---

        # Arch Linux Notes

        Use pacman for package management.
        AUR for community packages.
    """))

    return kb_dir


class TestSlugify:
    def test_basic(self):
        from kb_add import slugify
        assert slugify("Docker Setup") == "docker-setup"

    def test_special_chars(self):
        from kb_add import slugify
        assert slugify("What's New? (2026)") == "whats-new-2026"

    def test_extra_spaces(self):
        from kb_add import slugify
        assert slugify("  Multiple   Spaces  ") == "multiple-spaces"

    def test_already_slugified(self):
        from kb_add import slugify
        assert slugify("already-a-slug") == "already-a-slug"


class TestParseTags:
    def test_comma_separated(self):
        from kb_add import parse_tags
        assert parse_tags("music, gear, shopping") == ["music", "gear", "shopping"]

    def test_yaml_format(self):
        from kb_add import parse_tags
        assert parse_tags("[music, gear]") == ["music", "gear"]

    def test_empty(self):
        from kb_add import parse_tags
        assert parse_tags("") == []
        assert parse_tags("[]") == []


class TestFindDuplicates:
    def test_exact_filename_match(self, kb_with_notes):
        from kb_add import find_duplicates
        dupes = find_duplicates("Docker Setup", [])
        assert any("docker-setup.md" in d[0] for d in dupes)

    def test_exact_title_match(self, kb_with_notes):
        from kb_add import find_duplicates
        dupes = find_duplicates("Tech Gear", [])
        assert any("tech-gear.md" in d[0] for d in dupes)

    def test_no_duplicates(self, kb_with_notes):
        from kb_add import find_duplicates
        dupes = find_duplicates("Completely New Topic", [])
        assert dupes == []


class TestFindRelatedNotes:
    def test_finds_by_tag_overlap(self, kb_with_notes):
        from kb_add import find_related_notes
        related = find_related_notes(
            "Container Orchestration",
            ["devops", "docker"],
            "Kubernetes and Docker Swarm.",
        )
        # Should find docker-setup.md (shared tags: devops, docker)
        assert "docker-setup.md" in related

    def test_finds_by_title_words(self, kb_with_notes):
        from kb_add import find_related_notes
        related = find_related_notes(
            "Docker Networking",
            [],
            "How container networking works.",
        )
        # Should find docker-setup.md (title word "docker" in content)
        assert "docker-setup.md" in related

    def test_no_matches(self, kb_with_notes):
        from kb_add import find_related_notes
        related = find_related_notes(
            "Quantum Physics",
            ["science"],
            "Superposition and entanglement.",
        )
        # No devops/music notes should match quantum physics
        assert len(related) == 0

    def test_excludes_target_file(self, kb_with_notes):
        from kb_add import find_related_notes
        related = find_related_notes(
            "Docker Setup",
            ["devops", "docker"],
            "Setting up Docker.",
            exclude_file="docker-setup.md",
        )
        assert "docker-setup.md" not in related

    def test_excludes_index(self, kb_with_notes):
        from kb_add import find_related_notes
        related = find_related_notes(
            "Index Test",
            ["index"],
            "Testing index exclusion.",
        )
        assert "_index.md" not in related


class TestCreateNote:
    def test_creates_file(self, kb_dir):
        from kb_add import create_note

        path = create_note("Test Note", ["test"], "A test", "Content here.", [])
        assert path.exists()
        assert path.name == "test-note.md"

    def test_frontmatter(self, kb_dir):
        from kb_add import create_note

        path = create_note("My Topic", ["tag1", "tag2"], "Summary", "Body.", [])
        text = path.read_text()
        assert "title: My Topic" in text
        assert "tags: [tag1, tag2]" in text
        assert "summary: Summary" in text

    def test_related_links_in_frontmatter(self, kb_dir):
        from kb_add import create_note

        path = create_note("Linked Note", [], "", "Content.",
                           ["other.md", "another.md"])
        text = path.read_text()
        assert "related: [other.md, another.md]" in text

    def test_see_also_section(self, kb_with_notes):
        from kb_add import create_note

        path = create_note("New Note", [], "", "Content.",
                           ["docker-setup.md"])
        text = path.read_text()
        assert "## See Also" in text
        assert "[Docker Setup](docker-setup.md)" in text

    def test_custom_filename(self, kb_dir):
        from kb_add import create_note

        path = create_note("Title", [], "", "Content.", [],
                           filename="custom-name.md")
        assert path.name == "custom-name.md"


class TestUpdateRelatedNote:
    def test_adds_backlink(self, kb_with_notes):
        from kb_add import update_related_note

        note_path = kb_with_notes / "docker-setup.md"
        updated = update_related_note(note_path, "new-note.md")
        assert updated is True

        text = note_path.read_text()
        assert "new-note.md" in text

    def test_no_duplicate_backlink(self, kb_with_notes):
        from kb_add import update_related_note

        note_path = kb_with_notes / "arch-linux.md"
        # docker-setup.md is already in related
        updated = update_related_note(note_path, "docker-setup.md")
        assert updated is False

    def test_adds_to_existing_related(self, kb_with_notes):
        from kb_add import update_related_note, parse_frontmatter

        note_path = kb_with_notes / "arch-linux.md"
        update_related_note(note_path, "new-note.md")

        fm = parse_frontmatter(note_path)
        assert "docker-setup.md" in fm["related"]
        assert "new-note.md" in fm["related"]


class TestBuildFrontmatter:
    def test_basic(self):
        from kb_add import build_frontmatter

        fm = build_frontmatter("Title", ["t1", "t2"], "A summary", ["other.md"])
        assert "title: Title" in fm
        assert "tags: [t1, t2]" in fm
        assert "summary: A summary" in fm
        assert "related: [other.md]" in fm

    def test_empty_tags(self):
        from kb_add import build_frontmatter

        fm = build_frontmatter("Title", [], "", [])
        assert "tags: []" in fm
        assert "related: []" in fm


class TestCmdAdd:
    def test_full_flow(self, kb_with_notes, capsys):
        """End-to-end: create a note that auto-links to related notes."""
        import argparse
        from kb_add import cmd_add

        args = argparse.Namespace(
            title="Docker Volumes",
            tags="devops, docker",
            summary="Managing Docker volumes",
            content="Use named volumes for persistence. Bind mounts for development.",
            filename=None,
            dry_run=False,
            force=False,
        )
        cmd_add(args)

        output = capsys.readouterr().out
        assert "Created" in output
        assert "docker-volumes.md" in output

        # Verify file was created
        created = kb_with_notes / "docker-volumes.md"
        assert created.exists()

        # Should have found docker-setup.md as related
        text = created.read_text()
        assert "docker-setup.md" in text

    def test_dry_run(self, kb_with_notes, capsys):
        import argparse
        from kb_add import cmd_add

        args = argparse.Namespace(
            title="Docker Volumes",
            tags="devops, docker",
            summary="Managing Docker volumes",
            content="Use named volumes.",
            filename=None,
            dry_run=True,
            force=False,
        )
        cmd_add(args)

        output = capsys.readouterr().out
        assert "Dry run" in output
        assert "docker-volumes.md" in output

        # File should NOT be created
        assert not (kb_with_notes / "docker-volumes.md").exists()

    def test_duplicate_blocks(self, kb_with_notes):
        import argparse
        from kb_add import cmd_add

        args = argparse.Namespace(
            title="Docker Setup",
            tags="devops",
            summary="Duplicate",
            content="Some content.",
            filename=None,
            dry_run=False,
            force=False,
        )
        with pytest.raises(SystemExit):
            cmd_add(args)

    def test_duplicate_force(self, kb_with_notes, capsys):
        import argparse
        from kb_add import cmd_add

        args = argparse.Namespace(
            title="Docker Setup",
            tags="devops",
            summary="Forced duplicate",
            content="New content.",
            filename="docker-setup-v2",
            dry_run=False,
            force=True,
        )
        cmd_add(args)

        output = capsys.readouterr().out
        assert "Created" in output
        assert "docker-setup-v2.md" in output
