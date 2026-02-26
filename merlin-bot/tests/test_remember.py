"""Tests for remember.py — user memory management."""

import textwrap
from pathlib import Path

import pytest


USER_MD_TEMPLATE = textwrap.dedent("""\
    # User Memory

    Facts about the user that Merlin should always remember.

    ## Identity

    - Name: (to be filled in)
    - Timezone: (to be filled in)

    ## Preferences

    - Communication style: (to be filled in)
    - Languages: (to be filled in)

    ## Context

    - Current projects: (to be filled in)
    - Interests: (to be filled in)

    ## Notes

    (Add important facts here as they come up in conversation)
""")

USER_MD_WITH_FACTS = textwrap.dedent("""\
    # User Memory

    Facts about the user that Merlin should always remember.

    ## Identity

    - Name: Alex
    - Timezone: Europe/Paris

    ## Preferences

    - Likes concise responses

    ## Context

    - Working on Merlin bot

    ## Notes

    - Prefers flat directory structures
""")


@pytest.fixture
def user_md(tmp_path, monkeypatch):
    """Create a temporary user.md with the template content."""
    import remember

    md = tmp_path / "user.md"
    md.write_text(USER_MD_TEMPLATE)

    monkeypatch.setattr(remember, "USER_MD", md)
    return md


@pytest.fixture
def user_md_with_facts(tmp_path, monkeypatch):
    """Create a user.md with existing facts."""
    import remember

    md = tmp_path / "user.md"
    md.write_text(USER_MD_WITH_FACTS)

    monkeypatch.setattr(remember, "USER_MD", md)
    return md


class TestGetSections:
    def test_finds_all_sections(self, user_md):
        from remember import get_sections

        text = user_md.read_text()
        sections = get_sections(text)
        assert "identity" in sections
        assert "preferences" in sections
        assert "context" in sections
        assert "notes" in sections

    def test_section_ranges(self, user_md):
        from remember import get_sections

        text = user_md.read_text()
        sections = get_sections(text)
        lines = text.splitlines()

        for name, (start, end) in sections.items():
            assert lines[start].startswith("## ")
            assert start < end


class TestAddFact:
    def test_add_to_default_section(self, user_md):
        from remember import add_fact

        result = add_fact("Likes coffee")
        assert "Notes" in result
        assert "Likes coffee" in result

        text = user_md.read_text()
        assert "- Likes coffee" in text

    def test_add_to_identity(self, user_md):
        from remember import add_fact

        result = add_fact("Name: Alex", section="identity")
        assert "Identity" in result

        text = user_md.read_text()
        assert "- Name: Alex" in text
        # Placeholder should be removed
        assert "(to be filled in)" not in text.split("## Preferences")[0]

    def test_add_to_preferences(self, user_md):
        from remember import add_fact

        add_fact("Prefers dark mode", section="preferences")
        text = user_md.read_text()
        assert "- Prefers dark mode" in text

    def test_add_to_context(self, user_md):
        from remember import add_fact

        add_fact("Working on Merlin bot", section="context")
        text = user_md.read_text()
        assert "- Working on Merlin bot" in text

    def test_removes_placeholders(self, user_md):
        from remember import add_fact

        add_fact("Name: Alex", section="identity")
        text = user_md.read_text()

        # Only the identity section should have placeholders removed
        identity_section = text.split("## Identity")[1].split("## Preferences")[0]
        assert "(to be filled in)" not in identity_section

        # Other sections should still have placeholders
        prefs_section = text.split("## Preferences")[1].split("## Context")[0]
        assert "(to be filled in)" in prefs_section

    def test_append_to_existing_facts(self, user_md_with_facts):
        from remember import add_fact

        add_fact("Languages: French, English", section="identity")
        text = user_md_with_facts.read_text()

        assert "- Name: Alex" in text
        assert "- Timezone: Europe/Paris" in text
        assert "- Languages: French, English" in text

    def test_auto_adds_bullet(self, user_md):
        from remember import add_fact

        add_fact("No bullet here", section="notes")
        text = user_md.read_text()
        assert "- No bullet here" in text

    def test_preserves_existing_bullet(self, user_md):
        from remember import add_fact

        add_fact("- Already has bullet", section="notes")
        text = user_md.read_text()
        # Should NOT double the bullet
        assert "- - Already has bullet" not in text
        assert "- Already has bullet" in text

    def test_multiple_adds_to_same_section(self, user_md):
        from remember import add_fact

        add_fact("Fact one", section="notes")
        add_fact("Fact two", section="notes")
        add_fact("Fact three", section="notes")

        text = user_md.read_text()
        assert "- Fact one" in text
        assert "- Fact two" in text
        assert "- Fact three" in text

    def test_file_not_found(self, tmp_path, monkeypatch):
        import remember
        from remember import add_fact as _add_fact

        monkeypatch.setattr(remember, "USER_MD", tmp_path / "nonexistent.md")
        result = _add_fact("Something")
        assert "Error" in result


class TestListFacts:
    def test_list_empty(self, user_md):
        from remember import list_facts

        output = list_facts()
        assert "User Memory" in output
        assert "(empty)" in output

    def test_list_with_facts(self, user_md_with_facts):
        from remember import list_facts

        output = list_facts()
        assert "Name: Alex" in output
        assert "Timezone: Europe/Paris" in output
        assert "Likes concise responses" in output
        assert "Working on Merlin bot" in output
        assert "flat directory" in output

    def test_file_not_found(self, tmp_path, monkeypatch):
        import remember
        from remember import list_facts as _list_facts

        monkeypatch.setattr(remember, "USER_MD", tmp_path / "nonexistent.md")
        output = _list_facts()
        assert "Error" in output


class TestCmdAdd:
    def test_cli_add(self, user_md, capsys):
        import argparse
        from remember import cmd_add

        args = argparse.Namespace(fact="Test fact", section="notes")
        cmd_add(args)

        output = capsys.readouterr().out
        assert "Added" in output
        assert "Test fact" in output

    def test_cli_add_default_section(self, user_md, capsys):
        import argparse
        from remember import cmd_add

        args = argparse.Namespace(fact="Default section fact", section=None)
        cmd_add(args)

        output = capsys.readouterr().out
        assert "Notes" in output


class TestCmdList:
    def test_cli_list(self, user_md_with_facts, capsys):
        import argparse
        from remember import cmd_list

        args = argparse.Namespace()
        cmd_list(args)

        output = capsys.readouterr().out
        assert "User Memory" in output
        assert "Alex" in output
