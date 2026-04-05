"""Tests for merlin-bot EXTENSION_META and extension API integration — Phase 6."""

import pytest
from fastapi.testclient import TestClient

import main as app_mod


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    """Disable auth for all route tests."""
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


@pytest.fixture
def client():
    """TestClient for the app."""
    with TestClient(app_mod.app) as c:
        yield c


def _bot_loaded():
    info = app_mod.extension_registry.get("merlin-bot")
    return info and info.loaded


# ---------------------------------------------------------------------------
# T22: EXTENSION_META tests
# ---------------------------------------------------------------------------


class TestExtensionMeta:
    def test_merlin_bot_has_extension_meta(self):
        """merlin_bot module exports EXTENSION_META dict."""
        import merlin_bot

        assert hasattr(merlin_bot, "EXTENSION_META")
        meta = merlin_bot.EXTENSION_META
        assert isinstance(meta, dict)

    def test_extension_meta_has_name(self):
        import merlin_bot

        assert merlin_bot.EXTENSION_META["name"] == "Merlin Bot"

    def test_extension_meta_has_description(self):
        import merlin_bot

        assert "description" in merlin_bot.EXTENSION_META
        assert len(merlin_bot.EXTENSION_META["description"]) > 0

    def test_extension_meta_has_icon(self):
        import merlin_bot

        assert "icon" in merlin_bot.EXTENSION_META
        assert "<svg" in merlin_bot.EXTENSION_META["icon"]

    def test_extension_meta_has_config_fields(self):
        import merlin_bot

        fields = merlin_bot.EXTENSION_META["config_fields"]
        assert isinstance(fields, list)
        assert len(fields) == 2

    def test_config_fields_discord_bot_token(self):
        import merlin_bot

        fields = merlin_bot.EXTENSION_META["config_fields"]
        token_field = next(f for f in fields if f["key"] == "DISCORD_BOT_TOKEN")
        assert token_field["secret"] is True
        assert token_field["required"] is True
        assert "label" in token_field

    def test_config_fields_discord_channel_ids(self):
        import merlin_bot

        fields = merlin_bot.EXTENSION_META["config_fields"]
        channel_field = next(f for f in fields if f["key"] == "DISCORD_CHANNEL_IDS")
        assert channel_field["secret"] is False
        assert channel_field["required"] is True
        assert "label" in channel_field


class TestExtensionsAPIBotMeta:
    def test_extensions_api_returns_bot_with_config_fields(self, client):
        """GET /api/extensions includes merlin-bot with config_fields in meta."""
        if not _bot_loaded():
            pytest.skip("Bot extension not loaded")
        resp = client.get("/api/extensions")
        assert resp.status_code == 200
        data = resp.json()
        bot_ext = next((e for e in data if e["id"] == "merlin-bot"), None)
        assert bot_ext is not None
        assert "meta" in bot_ext
        assert "config_fields" in bot_ext["meta"]
        fields = bot_ext["meta"]["config_fields"]
        keys = [f["key"] for f in fields]
        assert "DISCORD_BOT_TOKEN" in keys
        assert "DISCORD_CHANNEL_IDS" in keys

    def test_extensions_api_bot_meta_name(self, client):
        """GET /api/extensions bot entry has correct name in meta."""
        if not _bot_loaded():
            pytest.skip("Bot extension not loaded")
        resp = client.get("/api/extensions")
        data = resp.json()
        bot_ext = next((e for e in data if e["id"] == "merlin-bot"), None)
        assert bot_ext is not None
        assert bot_ext["meta"]["name"] == "Merlin Bot"


class TestDiscordDirectives:
    def test_discord_directives_file_exists(self):
        """discord_directives.md still exists (kept for reference)."""
        from pathlib import Path

        directives = Path(__file__).parent.parent.parent / "merlin-bot" / "discord_directives.md"
        assert directives.exists()

    def test_directives_not_injected(self):
        """discord_directives.md is NOT injected into engine — engine has no Discord notion."""
        import merlin_bot

        # The _DISCORD_DIRECTIVES constant was removed; engine gets no extra_system_prompts
        assert not hasattr(merlin_bot, "_DISCORD_DIRECTIVES")
