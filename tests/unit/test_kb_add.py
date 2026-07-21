"""Tests for notes/commands/kb.py — KB add, index, and check commands."""

import argparse
import textwrap

import pytest


def _add_args(**overrides):
    """Namespace for cmd_add with sensible defaults."""
    defaults = dict(
        type="reference",
        title="Test Note",
        tags=None,
        description="",
        resource=None,
        content="Some content.",
        filename=None,
        dry_run=False,
        force=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def kb_dir(tmp_path, monkeypatch):
    """Create a temporary KB directory and patch the kb module to use it."""
    from notes.commands import kb as kb_mod

    kb = tmp_path / "kb"
    kb.mkdir()
    media = tmp_path / "media"
    media.mkdir()

    monkeypatch.setattr(kb_mod, "KB_DIR", kb)
    monkeypatch.setattr(kb_mod, "MEDIA_DIR", media)
    return kb


@pytest.fixture
def kb_with_notes(kb_dir):
    """KB populated with several conformant test notes."""
    (kb_dir / "docker-setup.md").write_text(
        textwrap.dedent("""\
        ---
        type: technique
        title: Docker Setup
        description: How to set up Docker
        tags: [devops, docker]
        created: 2026-01-15
        updated: 2026-01-15
        ---

        # Docker Setup

        Install Docker on Arch Linux using pacman.
    """)
    )

    (kb_dir / "tech-gear.md").write_text(
        textwrap.dedent("""\
        ---
        type: reference
        title: Tech Gear
        description: Notes on tech equipment
        tags: [tech, personal]
        created: 2026-01-20
        updated: 2026-01-20
        ---

        # Tech Gear

        Looking for a good mechanical keyboard.
        Extends [Docker Setup](docker-setup.md) with hardware context.
    """)
    )

    return kb_dir


class TestSlugify:
    def test_basic(self):
        from notes.commands.kb import slugify

        assert slugify("Docker Setup") == "docker-setup"

    def test_special_chars(self):
        from notes.commands.kb import slugify

        assert slugify("What's New? (2026)") == "whats-new-2026"


class TestParseTags:
    def test_comma_separated(self):
        from notes.commands.kb import parse_tags

        assert parse_tags("music, gear, shopping") == ["music", "gear", "shopping"]

    def test_yaml_format(self):
        from notes.commands.kb import parse_tags

        assert parse_tags("[music, gear]") == ["music", "gear"]

    def test_empty(self):
        from notes.commands.kb import parse_tags

        assert parse_tags("") == []
        assert parse_tags("[]") == []


class TestFrontmatterParsing:
    def test_reads_fields(self, kb_with_notes):
        from notes.commands.kb import parse_frontmatter

        meta = parse_frontmatter(kb_with_notes / "docker-setup.md")
        assert meta["type"] == "technique"
        assert meta["title"] == "Docker Setup"
        assert meta["tags"] == ["devops", "docker"]

    def test_unparseable_reports_error(self, kb_dir):
        from notes.commands.kb import read_frontmatter

        bad = kb_dir / "bad.md"
        bad.write_text("---\ntitle: [unclosed\n---\n\nBody\n")
        meta, error = read_frontmatter(bad)
        assert meta is None
        assert error

    def test_no_frontmatter(self, kb_dir):
        from notes.commands.kb import read_frontmatter

        bare = kb_dir / "bare.md"
        bare.write_text("# Just a heading\n")
        meta, error = read_frontmatter(bare)
        assert meta is None
        assert "no frontmatter" in error


class TestBodyKbLinks:
    def test_finds_relative_md_links(self):
        from notes.commands.kb import body_kb_links

        body = "Extends [Docker Setup](docker-setup.md) with more detail."
        links = body_kb_links(body)
        assert [(label, target) for label, target, _ in links] == [
            ("Docker Setup", "docker-setup.md")
        ]

    def test_ignores_external_and_non_md(self):
        from notes.commands.kb import body_kb_links

        body = "See [site](https://example.com/page.md) and [img](media/x.png)."
        assert body_kb_links(body) == []

    def test_strips_bundle_absolute_prefix(self):
        from notes.commands.kb import body_kb_links

        links = body_kb_links("See [X](/other-note.md).")
        assert links[0][1] == "other-note.md"


class TestFindDuplicates:
    def test_exact_filename_match(self, kb_with_notes):
        from notes.commands.kb import find_duplicates

        dupes = find_duplicates("Docker Setup")
        assert any("docker-setup.md" in d[0] for d in dupes)

    def test_exact_title_match(self, kb_with_notes):
        from notes.commands.kb import find_duplicates

        dupes = find_duplicates("Tech Gear")
        assert any("tech-gear.md" in d[0] for d in dupes)

    def test_no_duplicates(self, kb_with_notes):
        from notes.commands.kb import find_duplicates

        assert find_duplicates("Completely New Topic") == []


class TestBuildFrontmatter:
    def test_basic(self):
        from notes.commands.kb import build_frontmatter

        fm = build_frontmatter(
            "technique", "Title", "A description", ["t1", "t2"], None
        )
        assert "type: technique" in fm
        assert "title: Title" in fm
        assert "description: A description" in fm
        assert "tags: [t1, t2]" in fm
        assert "created: " in fm
        assert "updated: " in fm
        assert "related" not in fm
        assert "summary" not in fm

    def test_resource(self):
        from notes.commands.kb import build_frontmatter

        fm = build_frontmatter("transcript", "T", "", [], "https://youtube.com/x")
        assert "resource: https://youtube.com/x" in fm

    def test_no_resource_line_when_absent(self):
        from notes.commands.kb import build_frontmatter

        assert "resource" not in build_frontmatter("reference", "T", "", [], None)


class TestCreateNote:
    def test_creates_file(self, kb_dir):
        from notes.commands.kb import create_note

        path = create_note(
            "reference", "Test Note", "A test", ["test"], "Content here."
        )
        assert path.exists()
        assert path.name == "test-note.md"

    def test_no_links_generated(self, kb_with_notes):
        from notes.commands.kb import create_note

        path = create_note("reference", "New Note", "", [], "Content.")
        text = path.read_text()
        assert "See Also" not in text
        assert "related" not in text

    def test_custom_filename(self, kb_dir):
        from notes.commands.kb import create_note

        path = create_note(
            "reference", "Title", "", [], "Content.", filename="custom-name.md"
        )
        assert path.name == "custom-name.md"


class TestBuildIndex:
    def test_groups_by_type(self, kb_with_notes):
        from notes.commands.kb import build_index

        index = build_index()
        assert "## reference" in index
        assert "## technique" in index
        assert "* [Docker Setup](docker-setup.md) - How to set up Docker" in index
        assert 'okf_version: "0.1"' in index

    def test_untyped_grouped_last(self, kb_with_notes):
        from notes.commands.kb import build_index

        (kb_with_notes / "old-note.md").write_text(
            "---\ntitle: Old Note\nsummary: Legacy\n---\n\n# Old Note\n\nBody.\n"
        )
        index = build_index()
        assert "## (untyped)" in index
        assert index.index("## technique") < index.index("## (untyped)")
        # summary used as description fallback
        assert "* [Old Note](old-note.md) - Legacy" in index

    def test_cmd_index_writes_file(self, kb_with_notes, capsys):
        from notes.commands.kb import cmd_index

        cmd_index(argparse.Namespace())
        assert (kb_with_notes / "index.md").exists()
        assert "Index updated" in capsys.readouterr().out


class TestCheck:
    def _write_index(self, kb):
        from notes.commands.kb import build_index

        (kb / "index.md").write_text(build_index())

    def test_conformant_kb_passes(self, kb_with_notes, capsys):
        from notes.commands.kb import cmd_check

        self._write_index(kb_with_notes)
        cmd_check(argparse.Namespace())
        assert "KB conforms" in capsys.readouterr().out

    def test_missing_type(self, kb_with_notes):
        from notes.commands.kb import check_findings

        (kb_with_notes / "untyped.md").write_text(
            "---\ntitle: Untyped\ndescription: x\n---\n\n# Untyped\n\nBody.\n"
        )
        findings = check_findings()
        assert any("missing required field: type" in msg for _, msg in findings)

    def test_legacy_fields(self, kb_with_notes):
        from notes.commands.kb import check_findings

        (kb_with_notes / "legacy.md").write_text(
            "---\ntype: reference\ntitle: L\nsummary: s\nrelated: [x.md]\n---\n\n# L\n\nBody.\n"
        )
        findings = check_findings()
        messages = [msg for name, msg in findings if name == "legacy.md"]
        assert any("legacy field: related" in m for m in messages)
        assert any("legacy field: summary" in m for m in messages)

    def test_broken_link(self, kb_with_notes):
        from notes.commands.kb import check_findings

        (kb_with_notes / "linker.md").write_text(
            "---\ntype: reference\ntitle: Linker\ndescription: x\n---\n\n"
            "# Linker\n\nExtends [Ghost](ghost-note.md) because reasons.\n"
        )
        findings = check_findings()
        assert any("broken link: (ghost-note.md)" in msg for _, msg in findings)

    def test_bare_link(self, kb_with_notes):
        from notes.commands.kb import check_findings

        (kb_with_notes / "bare.md").write_text(
            "---\ntype: reference\ntitle: Bare\ndescription: x\n---\n\n"
            "# Bare\n\n- [Docker Setup](docker-setup.md)\n"
        )
        findings = check_findings()
        assert any("bare link" in msg for _, msg in findings)

    def test_described_link_passes(self, kb_with_notes):
        from notes.commands.kb import check_findings

        (kb_with_notes / "described.md").write_text(
            "---\ntype: reference\ntitle: Described\ndescription: x\n---\n\n"
            "# Described\n\n- [Docker Setup](docker-setup.md) - the base this builds on\n"
        )
        findings = check_findings()
        assert not any(
            "bare link" in msg for name, msg in findings if name == "described.md"
        )

    def test_legacy_index_flagged(self, kb_with_notes):
        from notes.commands.kb import check_findings

        (kb_with_notes / "_index.md").write_text("# Old index\n")
        findings = check_findings()
        assert any(name == "_index.md" for name, _ in findings)

    def test_missing_and_stale_index(self, kb_with_notes):
        from notes.commands.kb import check_findings

        findings = check_findings()
        assert any("missing" in msg for name, msg in findings if name == "index.md")

        (kb_with_notes / "index.md").write_text("stale content\n")
        findings = check_findings()
        assert any("stale" in msg for name, msg in findings if name == "index.md")

    def test_orphaned_media(self, kb_with_notes, monkeypatch):
        from notes.commands import kb as kb_mod

        (kb_mod.MEDIA_DIR / "photo.jpeg").write_text("binary-ish")
        findings = kb_mod.check_findings()
        assert any("orphaned" in msg for name, msg in findings if "photo" in name)

    def test_owned_media_passes(self, kb_with_notes):
        from notes.commands import kb as kb_mod

        (kb_mod.MEDIA_DIR / "photo.jpeg").write_text("binary-ish")
        (kb_with_notes / "photo-note.md").write_text(
            "---\ntype: reference\ntitle: Photo\ndescription: a photo\n"
            "resource: media/photo.jpeg\n---\n\n# Photo\n\nWhat it shows.\n"
        )
        findings = kb_mod.check_findings()
        assert not any("orphaned" in msg for _, msg in findings)

    def test_check_exits_on_findings(self, kb_with_notes):
        from notes.commands.kb import cmd_check

        with pytest.raises(SystemExit):
            cmd_check(argparse.Namespace())  # index.md missing


class TestCmdAdd:
    def test_full_flow(self, kb_with_notes, capsys):
        """Create a note: file written, index refreshed, zero links invented."""
        from notes.commands.kb import cmd_add

        cmd_add(
            _add_args(
                type="technique",
                title="Docker Volumes",
                tags="devops, docker",
                description="Managing Docker volumes",
                content="Use named volumes for persistence.",
            )
        )

        output = capsys.readouterr().out
        assert "Created" in output
        assert "docker-volumes.md" in output
        assert "Index refreshed" in output

        created = kb_with_notes / "docker-volumes.md"
        text = created.read_text()
        assert "type: technique" in text
        assert "description: Managing Docker volumes" in text
        assert "related" not in text
        assert "See Also" not in text
        # Index regenerated and includes the new note
        assert "docker-volumes.md" in (kb_with_notes / "index.md").read_text()

    def test_dry_run(self, kb_with_notes, capsys):
        from notes.commands.kb import cmd_add

        cmd_add(_add_args(title="Docker Volumes", dry_run=True))
        output = capsys.readouterr().out
        assert "Dry run" in output
        assert not (kb_with_notes / "docker-volumes.md").exists()

    def test_duplicate_blocks(self, kb_with_notes):
        from notes.commands.kb import cmd_add

        with pytest.raises(SystemExit):
            cmd_add(_add_args(title="Docker Setup"))

    def test_duplicate_force(self, kb_with_notes, capsys):
        from notes.commands.kb import cmd_add

        cmd_add(_add_args(title="Docker Setup", filename="docker-setup-v2", force=True))
        assert "docker-setup-v2.md" in capsys.readouterr().out

    def test_broken_link_blocks(self, kb_with_notes):
        from notes.commands.kb import cmd_add

        with pytest.raises(SystemExit):
            cmd_add(
                _add_args(
                    title="Bad Linker",
                    content="Extends [Ghost](missing-note.md) somehow.",
                )
            )

    def test_valid_link_passes(self, kb_with_notes, capsys):
        from notes.commands.kb import cmd_add

        cmd_add(
            _add_args(
                title="Good Linker",
                content="Extends [Docker Setup](docker-setup.md) with orchestration.",
            )
        )
        assert "Created" in capsys.readouterr().out

    def test_self_link_allowed(self, kb_with_notes, capsys):
        from notes.commands.kb import cmd_add

        cmd_add(
            _add_args(
                title="Self Referent",
                content="Anchor to [itself](self-referent.md) is fine.",
            )
        )
        assert "Created" in capsys.readouterr().out

    def test_new_type_notice(self, kb_with_notes, capsys):
        from notes.commands.kb import cmd_add

        cmd_add(_add_args(type="brand-new-type", title="Novel"))
        output = capsys.readouterr().out
        assert "New type" in output
        assert "technique" in output  # existing vocabulary listed

    def test_known_type_no_notice(self, kb_with_notes, capsys):
        from notes.commands.kb import cmd_add

        cmd_add(_add_args(type="technique", title="Known Type Note"))
        assert "New type" not in capsys.readouterr().out

    def test_new_tag_notice(self, kb_with_notes, capsys):
        from notes.commands.kb import cmd_add

        cmd_add(_add_args(title="Tagged", tags="devops, quantum-biology"))
        output = capsys.readouterr().out
        assert "New tag" in output
        assert "quantum-biology" in output
