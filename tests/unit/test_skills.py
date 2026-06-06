"""Tests for lib/skills.py — skill registry and canonical aggregation."""

from pathlib import Path

import pytest

import paths
from lib import skills


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_skill(
    base: Path,
    dir_name: str,
    *,
    name: str | None = None,
    description: str = "A test skill.",
    frontmatter: bool = True,
) -> Path:
    """Create <base>/skills/<dir_name>/SKILL.md and return the skill dir."""
    skill_dir = base / "skills" / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    if frontmatter:
        content = f"---\nname: {name or dir_name}\ndescription: {description}\n---\n\n# Body\n"
    else:
        content = "# No frontmatter here\n"
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_parses_name_and_description(self, tmp_path):
        skill_dir = make_skill(tmp_path, "alpha", description="Does alpha things.")
        fields = skills.parse_skill_frontmatter(skill_dir / "SKILL.md")
        assert fields["name"] == "alpha"
        assert fields["description"] == "Does alpha things."

    def test_missing_file(self, tmp_path):
        assert skills.parse_skill_frontmatter(tmp_path / "nope.md") == {}

    def test_no_frontmatter(self, tmp_path):
        skill_dir = make_skill(tmp_path, "bare", frontmatter=False)
        assert skills.parse_skill_frontmatter(skill_dir / "SKILL.md") == {}


# ---------------------------------------------------------------------------
# Registry build
# ---------------------------------------------------------------------------


class TestBuildRegistry:
    def test_builds_from_extension_and_user_sources(self, tmp_path):
        bot_dir = tmp_path / "merlin-bot"
        make_skill(bot_dir, "cron", description="Cron skill.")
        make_skill(bot_dir, "notes", description="Notes skill.")

        ext_dir = paths.extensions_dir() / "tasks"
        make_skill(ext_dir, "tasks", description="Tasks skill.")

        user_home = skills.user_skills_dir().parent
        make_skill(user_home, "teacher", description="Teacher skill.")

        registry = skills.build_registry({"merlin-bot": bot_dir, "tasks": ext_dir})

        assert sorted(registry) == ["cron", "notes", "tasks", "teacher"]
        assert registry["cron"].source == "merlin-bot"
        assert registry["tasks"].source == "tasks"
        assert registry["teacher"].source == "user"
        assert registry["teacher"].description == "Teacher skill."

    def test_frontmatter_name_wins_over_dir_name(self, tmp_path):
        ext = tmp_path / "ext"
        make_skill(ext, "some-dir", name="custom-name")
        registry = skills.build_registry({"ext": ext})
        assert "custom-name" in registry
        assert "some-dir" not in registry

    def test_skill_without_skill_md_ignored(self, tmp_path):
        ext = tmp_path / "ext"
        (ext / "skills" / "empty").mkdir(parents=True)
        assert skills.build_registry({"ext": ext}) == {}

    def test_name_conflict_first_source_wins(self, tmp_path, caplog):
        first = tmp_path / "first"
        second = tmp_path / "second"
        make_skill(first, "dup", description="From first.")
        make_skill(second, "dup", description="From second.")

        registry = skills.build_registry({"first": first, "second": second})
        assert registry["dup"].source == "first"
        assert "conflict" in caplog.text.lower()

    def test_missing_sources_are_fine(self, tmp_path):
        assert skills.build_registry({"ghost": tmp_path / "ghost"}) == {}

    def test_user_home_is_notes_dir_skills(self, tmp_path):
        assert skills.user_skills_dir() == paths.notes_dir() / "skills"


# ---------------------------------------------------------------------------
# Canonical aggregation
# ---------------------------------------------------------------------------


class TestRebuild:
    def test_creates_symlinks(self, tmp_path):
        bot_dir = tmp_path / "merlin-bot"
        cron_skill = make_skill(bot_dir, "cron")

        registry = skills.rebuild({"merlin-bot": bot_dir})

        link = skills.canonical_dir() / "cron"
        assert link.is_symlink()
        assert link.resolve() == cron_skill.resolve()
        assert (link / "SKILL.md").is_file()
        assert skills.get_registry() == registry

    def test_stale_symlinks_removed(self, tmp_path):
        bot_dir = tmp_path / "merlin-bot"
        make_skill(bot_dir, "cron")
        skills.rebuild({"merlin-bot": bot_dir})
        assert (skills.canonical_dir() / "cron").is_symlink()

        # Extension disabled: its skills disappear on rebuild
        skills.rebuild({})
        assert not (skills.canonical_dir() / "cron").exists()

    def test_unmanaged_entries_left_alone(self, tmp_path, caplog):
        canonical = skills.canonical_dir()
        canonical.mkdir(parents=True)
        unmanaged = canonical / "my-real-dir"
        unmanaged.mkdir()

        skills.rebuild({})
        assert unmanaged.is_dir()

    def test_collision_with_unmanaged_entry_warns(self, tmp_path, caplog):
        canonical = skills.canonical_dir()
        (canonical / "cron").mkdir(parents=True)

        bot_dir = tmp_path / "merlin-bot"
        make_skill(bot_dir, "cron")
        skills.rebuild({"merlin-bot": bot_dir})

        assert not (canonical / "cron").is_symlink()  # Unmanaged dir kept
        assert "unmanaged" in caplog.text.lower() or "name taken" in caplog.text.lower()

    def test_retarget_when_source_moves(self, tmp_path):
        first = tmp_path / "first"
        skill_a = make_skill(first, "dup")
        skills.rebuild({"first": first})

        second = tmp_path / "second"
        skill_b = make_skill(second, "dup")
        skills.rebuild({"second": second})

        link = skills.canonical_dir() / "dup"
        assert link.resolve() == skill_b.resolve() != skill_a.resolve()


# ---------------------------------------------------------------------------
# Real repo source: merlin-bot ships its skills
# ---------------------------------------------------------------------------


class TestRealBotSkills:
    def test_bot_skills_discovered(self):
        repo_bot = Path(__file__).parent.parent.parent / "merlin-bot"
        registry = skills.build_registry({"merlin-bot": repo_bot})
        for name in ("cron", "dashboard", "discord", "notes", "self-awareness"):
            assert name in registry, f"missing {name}"
            assert registry[name].description

    def test_teacher_not_in_repo(self):
        repo_bot = Path(__file__).parent.parent.parent / "merlin-bot"
        registry = skills.build_registry({"merlin-bot": repo_bot})
        assert "teacher" not in registry


# ---------------------------------------------------------------------------
# main.py source selection
# ---------------------------------------------------------------------------


class TestMainSources:
    def test_skill_source_dirs_tiers(self, tmp_path):
        import main

        saved = dict(main.extension_registry)
        try:
            main.extension_registry.clear()
            main.extension_registry["files"] = main.ExtensionInfo(
                id="files", tier="core", enabled=True, loaded=True, error=None
            )
            main.extension_registry["merlin-bot"] = main.ExtensionInfo(
                id="merlin-bot", tier="built-in", enabled=True, loaded=True, error=None
            )
            main.extension_registry["tasks"] = main.ExtensionInfo(
                id="tasks", tier="installed", enabled=True, loaded=True, error=None
            )
            main.extension_registry["broken"] = main.ExtensionInfo(
                id="broken", tier="installed", enabled=True, loaded=False, error="x"
            )

            sources = main._skill_source_dirs()
            assert sources == {
                "merlin-bot": paths.app_dir() / "merlin-bot",
                "tasks": paths.extensions_dir() / "tasks",
            }
        finally:
            main.extension_registry.clear()
            main.extension_registry.update(saved)


# ---------------------------------------------------------------------------
# Shim links (~/.agents/skills, ~/.claude/skills)
# ---------------------------------------------------------------------------


class TestShimLinks:
    @pytest.fixture(autouse=True)
    def _fake_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        self.home = home

    def _canonical_with_skill(self, tmp_path, name="cron"):
        bot_dir = tmp_path / "merlin-bot"
        make_skill(bot_dir, name)
        skills.rebuild({"merlin-bot": bot_dir})

    def test_creates_per_skill_links(self, tmp_path):
        self._canonical_with_skill(tmp_path, "cron")
        skills.sync_shim_links(skills.agents_skills_dir())

        link = self.home / ".agents" / "skills" / "cron"
        assert link.is_symlink()
        assert (link / "SKILL.md").is_file()
        # Raw target points into the canonical dir (single source of truth)
        import os as _os

        assert str(skills.canonical_dir()) in _os.readlink(link)

    def test_removes_stale_merlin_links_only(self, tmp_path):
        self._canonical_with_skill(tmp_path, "cron")
        target = skills.agents_skills_dir()
        skills.sync_shim_links(target)
        assert (target / "cron").is_symlink()

        # Skill disappears from canonical
        skills.rebuild({})
        skills.sync_shim_links(target)
        assert not (target / "cron").exists()

    def test_foreign_entries_untouched(self, tmp_path, caplog):
        foreign_dir = skills.agents_skills_dir() / "my-own-skill"
        foreign_dir.mkdir(parents=True)
        (foreign_dir / "SKILL.md").write_text("---\nname: my-own-skill\n---\n")

        foreign_link_target = tmp_path / "elsewhere"
        foreign_link_target.mkdir()
        foreign_link = skills.agents_skills_dir() / "linked-skill"
        foreign_link.symlink_to(foreign_link_target)

        self._canonical_with_skill(tmp_path, "cron")
        skills.sync_shim_links(skills.agents_skills_dir())

        assert foreign_dir.is_dir() and not foreign_dir.is_symlink()
        assert foreign_link.is_symlink()
        assert foreign_link.resolve() == foreign_link_target.resolve()

    def test_collision_with_foreign_entry_skipped(self, tmp_path, caplog):
        taken = skills.agents_skills_dir() / "cron"
        taken.mkdir(parents=True)

        self._canonical_with_skill(tmp_path, "cron")
        skills.sync_shim_links(skills.agents_skills_dir())

        assert taken.is_dir() and not taken.is_symlink()
        assert "skipped" in caplog.text.lower()

    def test_sync_interactive_shims_covers_both_scopes(self, tmp_path):
        self._canonical_with_skill(tmp_path, "cron")
        skills.sync_interactive_shims()
        assert (self.home / ".claude" / "skills" / "cron").is_symlink()
        assert (self.home / ".agents" / "skills" / "cron").is_symlink()


# ---------------------------------------------------------------------------
# Canonical read-back
# ---------------------------------------------------------------------------


class TestListCanonical:
    def test_lists_after_rebuild(self, tmp_path):
        bot_dir = tmp_path / "merlin-bot"
        make_skill(bot_dir, "cron", description="Cron skill.")
        skills.rebuild({"merlin-bot": bot_dir})

        specs = skills.list_canonical_skills()
        assert [s.name for s in specs] == ["cron"]
        assert specs[0].description == "Cron skill."

    def test_empty_when_no_canonical(self):
        assert skills.list_canonical_skills() == []
