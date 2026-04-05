"""Tests for the Extensions page API endpoints — Phase 2."""

import json
import os
from pathlib import Path
from unittest.mock import patch

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


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


class TestExtensionsPage:
    def test_get_extensions_page(self, client):
        """GET /extensions returns 200 with HTML."""
        resp = client.get("/extensions")
        assert resp.status_code == 200
        assert "Extensions" in resp.text


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


class TestExtensionsAPI:
    def test_get_extensions_api(self, client):
        """GET /api/extensions returns JSON with all extensions."""
        resp = client.get("/api/extensions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 3  # at least core: files, terminal, commits

    def test_extensions_api_structure(self, client):
        """Response has id, tier, enabled, loaded, error, meta per extension."""
        resp = client.get("/api/extensions")
        data = resp.json()
        for ext in data:
            assert "id" in ext
            assert "tier" in ext
            assert "enabled" in ext
            assert "loaded" in ext
            assert "error" in ext
            assert "meta" in ext

    def test_extensions_api_tiers(self, client):
        """Core items have tier='core', built-ins 'built-in', installed 'installed'."""
        resp = client.get("/api/extensions")
        data = resp.json()
        by_id = {e["id"]: e for e in data}
        assert by_id["files"]["tier"] == "core"
        assert by_id["terminal"]["tier"] == "core"
        assert by_id["commits"]["tier"] == "core"
        if "notes" in by_id:
            assert by_id["notes"]["tier"] == "built-in"
        if "video-scenes" in by_id:
            assert by_id["video-scenes"]["tier"] == "installed"


class TestToggle:
    def test_toggle_enable(self, client, tmp_path):
        """POST /api/extensions/notes/toggle flips state."""
        resp = client.post("/api/extensions/notes/toggle")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        # Verify written to extensions.json
        state_path = tmp_path / "extensions.json"
        assert state_path.exists()
        state = json.loads(state_path.read_text())
        assert "notes" in state

    def test_toggle_disable(self, client, tmp_path):
        """Toggle enabled extension → disabled in file."""
        # First toggle (notes defaults on, so this disables)
        resp1 = client.post("/api/extensions/notes/toggle")
        data1 = resp1.json()
        # Second toggle (re-enables)
        resp2 = client.post("/api/extensions/notes/toggle")
        data2 = resp2.json()
        assert data1["enabled"] != data2["enabled"]

    def test_toggle_core_rejected(self, client):
        """POST /api/extensions/files/toggle returns 400."""
        resp = client.post("/api/extensions/files/toggle")
        assert resp.status_code == 400

    def test_toggle_nonexistent_rejected(self, client):
        """POST /api/extensions/nonexistent/toggle returns 404."""
        resp = client.post("/api/extensions/nonexistent/toggle")
        assert resp.status_code == 404


class TestConfig:
    @pytest.fixture(autouse=True)
    def _add_config_fields(self):
        """Add config_fields to notes extension for testing."""
        info = app_mod.extension_registry.get("notes")
        if info:
            info.meta.setdefault("config_fields", [])
            info.meta["config_fields"].append({"key": "TEST_CONFIG_KEY", "label": "Test"})
        yield
        if info and "config_fields" in info.meta:
            info.meta["config_fields"] = [f for f in info.meta["config_fields"] if f["key"] != "TEST_CONFIG_KEY"]

    def test_config_save(self, client, tmp_path):
        """POST /api/extensions/notes/config writes to config.env."""
        config_path = tmp_path / "config.env"
        config_path.write_text("EXISTING_KEY=value\n")
        resp = client.post(
            "/api/extensions/notes/config",
            json={"TEST_CONFIG_KEY": "test-value"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_config_save_preserves_other_keys(self, client, tmp_path):
        """Saving config doesn't remove existing config keys."""
        config_path = tmp_path / "config.env"
        config_path.write_text("EXISTING_KEY=value\n")
        client.post(
            "/api/extensions/notes/config",
            json={"TEST_CONFIG_KEY": "test-value"},
        )
        content = config_path.read_text()
        assert "EXISTING_KEY=value" in content
        assert "TEST_CONFIG_KEY=test-value" in content

    def test_config_save_permissions(self, client, tmp_path):
        """config.env has 0600 after write."""
        config_path = tmp_path / "config.env"
        config_path.write_text("")
        client.post(
            "/api/extensions/notes/config",
            json={"TEST_CONFIG_KEY": "secret"},
        )
        mode = oct(config_path.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_config_save_masks_response(self, client, tmp_path):
        """Response doesn't return raw secret values."""
        config_path = tmp_path / "config.env"
        config_path.write_text("")
        resp = client.post(
            "/api/extensions/notes/config",
            json={"TEST_CONFIG_KEY": "super-secret"},
        )
        data = resp.json()
        assert data["values"]["TEST_CONFIG_KEY"] == "***"

    def test_config_rejects_undeclared_keys(self, client, tmp_path):
        """Keys not in config_fields are silently ignored."""
        config_path = tmp_path / "config.env"
        config_path.write_text("")
        client.post(
            "/api/extensions/notes/config",
            json={"UNDECLARED_KEY": "evil-value"},
        )
        content = config_path.read_text()
        assert "UNDECLARED_KEY" not in content

    def test_config_nonexistent_extension_404(self, client):
        """POST to nonexistent extension returns 404."""
        resp = client.post(
            "/api/extensions/nonexistent/config",
            json={"KEY": "value"},
        )
        assert resp.status_code == 404


class TestRestart:
    def test_restart_endpoint(self, client):
        """POST /api/restart returns 200."""
        with patch("subprocess.Popen") as mock_popen:
            resp = client.post("/api/restart")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
