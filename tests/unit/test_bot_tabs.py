"""Tests for Bot tab consolidation — Phase 4."""

import pytest
from fastapi.testclient import TestClient

import main as app_mod


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    import auth
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


@pytest.fixture
def client():
    with TestClient(app_mod.app) as c:
        yield c


class TestBotRoutes:
    def _bot_loaded(self):
        """Check if bot extension is loaded."""
        info = app_mod.extension_registry.get("merlin-bot")
        return info and info.loaded

    def test_bot_page_returns_200(self, client):
        if not self._bot_loaded():
            pytest.skip("Bot extension not loaded")
        resp = client.get("/bot")
        assert resp.status_code == 200

    def test_bot_performance_returns_200(self, client):
        if not self._bot_loaded():
            pytest.skip("Bot extension not loaded")
        resp = client.get("/bot/performance")
        assert resp.status_code == 200

    def test_bot_logs_returns_200(self, client):
        if not self._bot_loaded():
            pytest.skip("Bot extension not loaded")
        resp = client.get("/bot/logs")
        assert resp.status_code == 200

    def test_old_routes_gone(self, client):
        """GET /overview, /performance, /logs return 404."""
        for path in ("/overview", "/performance", "/logs"):
            resp = client.get(path)
            assert resp.status_code in (404, 405), f"{path} returned {resp.status_code}"


class TestBotNavItems:
    def test_single_nav_item(self):
        """MERLIN_APP_NAV_ITEMS has exactly 1 entry with url /bot."""
        from merlin_app import MERLIN_APP_NAV_ITEMS
        assert len(MERLIN_APP_NAV_ITEMS) == 1
        assert MERLIN_APP_NAV_ITEMS[0]["url"] == "/bot"


class TestBotTabsHTML:
    def test_bot_page_has_tabs(self, client):
        """Response HTML contains all three tab links."""
        info = app_mod.extension_registry.get("merlin-bot")
        if not (info and info.loaded):
            pytest.skip("Bot extension not loaded")
        resp = client.get("/bot")
        html = resp.text
        assert 'data-tab="overview"' in html
        assert 'data-tab="performance"' in html
        assert 'data-tab="logs"' in html
