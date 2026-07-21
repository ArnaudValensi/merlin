"""Tests for lib/agent_context.py — persona/context composition.

Layer accessors read from exactly one source each and degrade to None on
missing files; recipes compose the documented layer sets in order.
"""

import pytest

import lib.agent_context as ac


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated app dir, merlin home, and notes dir."""
    app = tmp_path / "app"
    home = tmp_path / "home"
    notes = tmp_path / "notes"
    for d in (app, home, notes):
        d.mkdir()
    monkeypatch.setattr(ac.paths, "app_dir", lambda: app)
    monkeypatch.setattr(ac.paths, "merlin_home", lambda: home)
    monkeypatch.setattr(ac.paths, "notes_dir", lambda: notes)
    return app, home, notes


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ---------------------------------------------------------------------------
# Layer accessors
# ---------------------------------------------------------------------------


class TestBrain:
    def test_reads_from_app_dir(self, env):
        app, _, _ = env
        _write(app / "agent" / "MERLIN.md", "# Merlin\n\nBrain content")
        assert ac.brain() == "# Merlin\n\nBrain content"

    def test_missing_returns_none(self, env):
        assert ac.brain() is None


class TestPersonality:
    def test_reads_from_new_path(self, env):
        _, home, _ = env
        _write(home / "personality.md", "Be awesome")
        assert ac.personality() == "Be awesome"

    def test_fallback_to_legacy_path(self, env):
        _, home, _ = env
        _write(home / "merlin-bot" / "personality.md", "Legacy voice")
        assert ac.personality() == "Legacy voice"

    def test_new_path_wins_over_legacy(self, env):
        _, home, _ = env
        _write(home / "personality.md", "New voice")
        _write(home / "merlin-bot" / "personality.md", "Legacy voice")
        assert ac.personality() == "New voice"

    def test_missing_returns_none(self, env):
        assert ac.personality() is None


class TestUserMemory:
    def test_reads_from_home(self, env):
        _, home, _ = env
        _write(home / "user.md", "User is a dev")
        assert ac.user_memory() == "# User Memory\n\nUser is a dev"

    def test_fallback_to_notes_dir(self, env):
        _, _, notes = env
        _write(notes / "user.md", "From notes")
        assert ac.user_memory() == "# User Memory\n\nFrom notes"

    def test_home_wins_over_notes(self, env):
        _, home, notes = env
        _write(home / "user.md", "From home")
        _write(notes / "user.md", "From notes")
        assert "From home" in ac.user_memory()

    def test_missing_returns_none(self, env):
        assert ac.user_memory() is None


class TestDiscordOverlay:
    def test_reads_from_app_dir(self, env):
        app, _, _ = env
        _write(app / "merlin-bot" / "discord_directives.md", "# Discord rules")
        assert ac.discord_overlay() == "# Discord rules"

    def test_missing_returns_none(self, env):
        assert ac.discord_overlay() is None


class TestLoadDegradation:
    def test_empty_file_returns_none(self, env):
        _, home, _ = env
        _write(home / "personality.md", "   \n  ")
        assert ac.personality() is None

    def test_unreadable_file_returns_none(self, env, monkeypatch):
        _, home, _ = env
        path = home / "personality.md"
        _write(path, "secret")
        path.chmod(0o000)
        try:
            assert ac.personality() is None
        finally:
            path.chmod(0o644)


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


@pytest.fixture
def all_layers(env):
    app, home, notes = env
    _write(app / "agent" / "MERLIN.md", "BRAIN")
    _write(home / "personality.md", "PERSONALITY")
    _write(home / "user.md", "USERFACTS")
    _write(app / "merlin-bot" / "discord_directives.md", "OVERLAY")
    return env


class TestCompose:
    def test_managed_assistant_all_layers_in_order(self, all_layers):
        result = ac.compose("managed-assistant")
        assert result == "BRAIN\n\nPERSONALITY\n\n# User Memory\n\nUSERFACTS\n\nOVERLAY"

    def test_headless_worker_brain_and_user_only(self, all_layers):
        result = ac.compose("headless-worker")
        assert result == "BRAIN\n\n# User Memory\n\nUSERFACTS"
        assert "PERSONALITY" not in result
        assert "OVERLAY" not in result

    def test_kb_index_included_when_present(self, all_layers):
        app, home, notes = all_layers
        _write(
            notes / "kb" / "index.md",
            '---\nokf_version: "0.1"\n---\n\n# Knowledge Base Index\n\n* [X](x.md) - a note\n',
        )
        for recipe in ("managed-assistant", "headless-worker"):
            result = ac.compose(recipe)
            assert "# Knowledge Base Index" in result
            assert "* [X](x.md) - a note" in result
            assert "okf_version" not in result


class TestKbIndex:
    def test_missing_returns_none(self, env):
        assert ac.kb_index() is None

    def test_strips_frontmatter(self, env):
        app, home, notes = env
        _write(
            notes / "kb" / "index.md",
            '---\nokf_version: "0.1"\n---\n\n# Knowledge Base Index\n\n* [X](x.md) - a note\n',
        )
        result = ac.kb_index()
        assert result.startswith("# Knowledge Base Index")
        assert "okf_version" not in result

    def test_no_frontmatter_passthrough(self, env):
        app, home, notes = env
        _write(notes / "kb" / "index.md", "# Knowledge Base Index\n\n* [X](x.md)\n")
        assert ac.kb_index().startswith("# Knowledge Base Index")

    def test_missing_layers_skipped(self, env):
        app, _, _ = env
        _write(app / "agent" / "MERLIN.md", "BRAIN")
        assert ac.compose("managed-assistant") == "BRAIN"

    def test_all_missing_returns_none(self, env):
        assert ac.compose("managed-assistant") is None
        assert ac.compose("headless-worker") is None

    def test_unknown_recipe_raises(self, env):
        with pytest.raises(ValueError, match="Unknown recipe"):
            ac.compose("nope")

    def test_layer_text_identical_to_accessors(self, all_layers):
        """Single-source check: recipes emit byte-for-byte the accessor text."""
        composed = ac.compose("managed-assistant")
        for layer in (
            ac.brain(),
            ac.personality(),
            ac.user_memory(),
            ac.discord_overlay(),
        ):
            assert layer in composed
