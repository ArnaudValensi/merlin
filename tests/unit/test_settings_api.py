"""Tests for the Settings page API endpoints — Phase 3."""

import json

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


class TestSettingsPage:
    def test_get_settings_page(self, client):
        """GET /settings returns 200 with HTML."""
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert "Settings" in resp.text


class TestSettingsAPI:
    def test_get_settings_api_masks_secrets(self, client, tmp_path):
        """GET /api/settings returns password_set/openai_key_set booleans, never raw values."""
        (tmp_path / "config.env").write_text("DASHBOARD_PASS=secret123\n")
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["password_set"] is True
        assert "secret123" not in json.dumps(data)

    def test_save_password(self, client, tmp_path):
        """POST /api/settings with password updates config.env."""
        (tmp_path / "config.env").write_text("")
        resp = client.post("/api/settings", json={"DASHBOARD_PASS": "newpass"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        content = (tmp_path / "config.env").read_text()
        assert "DASHBOARD_PASS=newpass" in content

    def test_save_openai_key(self, client, tmp_path):
        """POST /api/settings with OpenAI key updates config.env."""
        (tmp_path / "config.env").write_text("")
        resp = client.post("/api/settings", json={"OPENAI_API_KEY": "sk-test123"})
        assert resp.status_code == 200
        content = (tmp_path / "config.env").read_text()
        assert "OPENAI_API_KEY=sk-test123" in content

    def test_save_preserves_other_keys(self, client, tmp_path):
        """Saving password doesn't remove other config keys."""
        (tmp_path / "config.env").write_text("EXISTING_KEY=keep\nDASHBOARD_PASS=old\n")
        client.post("/api/settings", json={"DASHBOARD_PASS": "new"})
        content = (tmp_path / "config.env").read_text()
        assert "EXISTING_KEY=keep" in content
        assert "DASHBOARD_PASS=new" in content

    def test_clear_openai_key(self, client, tmp_path):
        """Sending empty value removes the key from config.env."""
        (tmp_path / "config.env").write_text("OPENAI_API_KEY=sk-old\nOTHER=val\n")
        client.post("/api/settings", json={"OPENAI_API_KEY": ""})
        content = (tmp_path / "config.env").read_text()
        assert "OPENAI_API_KEY" not in content
        assert "OTHER=val" in content

    def test_settings_file_permissions(self, client, tmp_path):
        """config.env has 0600 after write."""
        (tmp_path / "config.env").write_text("")
        client.post("/api/settings", json={"DASHBOARD_PASS": "test"})
        mode = oct((tmp_path / "config.env").stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_settings_rejects_unknown_keys(self, client, tmp_path):
        """POST /api/settings ignores unknown keys."""
        (tmp_path / "config.env").write_text("")
        client.post("/api/settings", json={"UNKNOWN_KEY": "value"})
        content = (tmp_path / "config.env").read_text()
        assert "UNKNOWN_KEY" not in content


class TestPublicUrlSetting:
    @pytest.fixture(autouse=True)
    def _clean_public_url_env(self):
        """The save handler mutates the real process env; keep tests isolated."""
        import os

        os.environ.pop("MERLIN_DASHBOARD_URL", None)
        yield
        os.environ.pop("MERLIN_DASHBOARD_URL", None)

    def test_get_settings_includes_public_url_state(
        self, client, tmp_path, monkeypatch
    ):
        # The module global is read at import; the dev shell may carry a token.
        monkeypatch.setattr(app_mod, "MERLIN_SAAS_TOKEN", "")
        (tmp_path / "config.env").write_text("")
        data = client.get("/api/settings").json()
        assert data["public_url"] == ""
        assert data["public_url_source"] in ("override", "saas", "slug", "ip")
        assert data["effective_public_url"].startswith("http")
        assert data["saas_mode"] is False

    def test_get_settings_reports_saas_mode(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(app_mod, "MERLIN_SAAS_TOKEN", "mrl_x")
        (tmp_path / "config.env").write_text("")
        assert client.get("/api/settings").json()["saas_mode"] is True

    def test_save_public_url_writes_config_and_env(self, client, tmp_path):
        import os

        (tmp_path / "config.env").write_text("")
        resp = client.post(
            "/api/settings", json={"MERLIN_DASHBOARD_URL": "https://me.example.com"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["public_url"] == "https://me.example.com"
        assert data["effective_public_url"] == "https://me.example.com"
        assert data["public_url_source"] == "override"
        content = (tmp_path / "config.env").read_text()
        assert "MERLIN_DASHBOARD_URL=https://me.example.com" in content
        # Live effect: the running process resolves the new value immediately
        # (config.env is loaded with setdefault semantics).
        assert os.environ["MERLIN_DASHBOARD_URL"] == "https://me.example.com"

    def test_save_public_url_normalizes_schemeless_and_slash(self, client, tmp_path):
        (tmp_path / "config.env").write_text("")
        resp = client.post(
            "/api/settings", json={"MERLIN_DASHBOARD_URL": "me.example.com:8443/"}
        )
        assert resp.json()["public_url"] == "http://me.example.com:8443"

    def test_clear_public_url_removes_config_and_env(self, client, tmp_path):
        import os

        (tmp_path / "config.env").write_text(
            "MERLIN_DASHBOARD_URL=https://old.example.com\nOTHER=keep\n"
        )
        os.environ["MERLIN_DASHBOARD_URL"] = "https://old.example.com"
        resp = client.post("/api/settings", json={"MERLIN_DASHBOARD_URL": ""})
        assert resp.status_code == 200
        content = (tmp_path / "config.env").read_text()
        assert "MERLIN_DASHBOARD_URL" not in content
        assert "OTHER=keep" in content
        assert "MERLIN_DASHBOARD_URL" not in os.environ

    def test_invalid_public_url_rejected(self, client, tmp_path):
        (tmp_path / "config.env").write_text("")
        resp = client.post("/api/settings", json={"MERLIN_DASHBOARD_URL": "http://"})
        assert resp.status_code == 422

    def test_settings_page_has_public_url_section(self, client):
        html = client.get("/settings").text
        assert "public-url-input" in html
        assert "Public URL" in html
