"""Tests for version and update API endpoints."""

import time
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import main as app_mod


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the version cache before each test."""
    app_mod._latest_tag_cache = (None, 0.0)
    yield
    app_mod._latest_tag_cache = (None, 0.0)


@pytest.fixture
def client():
    with TestClient(app_mod.app) as c:
        yield c


class TestVersionAPI:
    def test_returns_current_version(self, client):
        """GET /api/version returns current version."""
        with mock.patch.object(
            app_mod, "_get_latest_tag_cached", return_value="0.17.0"
        ):
            with mock.patch("cli.get_version", return_value="0.17.0"):
                resp = client.get("/api/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current"] == "0.17.0"
        assert data["latest"] == "0.17.0"

    def test_update_available_when_newer(self, client):
        """update_available is true when latest > current."""
        with mock.patch.object(
            app_mod, "_get_latest_tag_cached", return_value="0.18.0"
        ):
            with mock.patch("cli.get_version", return_value="0.17.0"):
                resp = client.get("/api/version")
        data = resp.json()
        assert data["update_available"] is True
        assert data["latest"] == "0.18.0"

    def test_no_update_when_same_version(self, client):
        """update_available is false when versions match."""
        with mock.patch.object(
            app_mod, "_get_latest_tag_cached", return_value="0.17.0"
        ):
            with mock.patch("cli.get_version", return_value="0.17.0"):
                resp = client.get("/api/version")
        data = resp.json()
        assert data["update_available"] is False

    def test_no_update_when_dev_mode(self, client):
        """Dev mode version with git describe suffix compares base version."""
        with mock.patch.object(
            app_mod, "_get_latest_tag_cached", return_value="0.17.0"
        ):
            with mock.patch("cli.get_version", return_value="0.17.0-3-gabcdef"):
                resp = client.get("/api/version")
        data = resp.json()
        assert data["current"] == "0.17.0-3-gabcdef"
        assert data["update_available"] is False

    def test_update_available_with_git_describe_suffix(self, client):
        """Git describe suffix should still detect newer versions."""
        with mock.patch.object(
            app_mod, "_get_latest_tag_cached", return_value="0.18.0"
        ):
            with mock.patch("cli.get_version", return_value="0.17.0-3-gabcdef"):
                resp = client.get("/api/version")
        data = resp.json()
        assert data["update_available"] is True

    def test_dev_mode_flag(self, client):
        """dev_mode reflects paths.is_dev_mode()."""
        with mock.patch.object(
            app_mod, "_get_latest_tag_cached", return_value="0.17.0"
        ):
            with mock.patch("cli.get_version", return_value="0.17.0"):
                with mock.patch("paths.is_dev_mode", return_value=True):
                    resp = client.get("/api/version")
        assert resp.json()["dev_mode"] is True

    def test_unknown_version_no_update(self, client):
        """No update when current version is 'unknown'."""
        with mock.patch.object(
            app_mod, "_get_latest_tag_cached", return_value="0.17.0"
        ):
            with mock.patch("cli.get_version", return_value="unknown"):
                resp = client.get("/api/version")
        data = resp.json()
        assert data["update_available"] is False

    def test_dev_version_no_update(self, client):
        """No update when current version is 'dev'."""
        with mock.patch.object(
            app_mod, "_get_latest_tag_cached", return_value="0.17.0"
        ):
            with mock.patch("cli.get_version", return_value="dev"):
                resp = client.get("/api/version")
        data = resp.json()
        assert data["update_available"] is False

    def test_github_api_failure(self, client):
        """Graceful handling when GitHub API fails."""
        with mock.patch.object(app_mod, "_get_latest_tag_cached", return_value=None):
            with mock.patch("cli.get_version", return_value="0.17.0"):
                resp = client.get("/api/version")
        data = resp.json()
        assert data["latest"] is None
        assert data["update_available"] is False


class TestVersionCache:
    def test_cache_reuses_result(self):
        """Cached result is returned within TTL."""
        with mock.patch("cli.fetch_latest_tag", return_value="0.17.0") as m:
            result1 = app_mod._get_latest_tag_cached()
            result2 = app_mod._get_latest_tag_cached()
        assert result1 == "0.17.0"
        assert result2 == "0.17.0"
        assert m.call_count == 1  # only called once

    def test_cache_expires(self):
        """Cache expires after TTL."""
        with mock.patch("cli.fetch_latest_tag", return_value="0.17.0"):
            app_mod._get_latest_tag_cached()

        # Simulate cache expiry
        tag, _ = app_mod._latest_tag_cache
        app_mod._latest_tag_cache = (tag, time.monotonic() - app_mod._CACHE_TTL - 1)

        with mock.patch("cli.fetch_latest_tag", return_value="0.18.0") as m:
            result = app_mod._get_latest_tag_cached()
        assert result == "0.18.0"
        assert m.call_count == 1


class TestUpdateAPI:
    def test_rejects_dev_mode(self, client):
        """POST /api/update returns error in dev mode."""
        with mock.patch("paths.is_dev_mode", return_value=True):
            resp = client.post("/api/update")
        data = resp.json()
        assert data["ok"] is False
        assert "dev mode" in data["error"]

    def test_rejects_when_up_to_date(self, client):
        """POST /api/update returns error when already up to date."""
        with mock.patch("paths.is_dev_mode", return_value=False):
            with mock.patch("cli.fetch_latest_tag", return_value="0.17.0"):
                with mock.patch("cli.get_version", return_value="0.17.0"):
                    resp = client.post("/api/update")
        data = resp.json()
        assert data["ok"] is False
        assert "up to date" in data["error"]

    def test_rejects_when_github_fails(self, client):
        """POST /api/update returns error when GitHub API fails."""
        with mock.patch("paths.is_dev_mode", return_value=False):
            with mock.patch("cli.fetch_latest_tag", return_value=None):
                resp = client.post("/api/update")
        data = resp.json()
        assert data["ok"] is False
        assert "fetch" in data["error"].lower()

    def test_successful_update(self, client, tmp_path):
        """POST /api/update downloads, swaps symlink, and triggers restart."""
        versions_dir = tmp_path / "versions"
        versions_dir.mkdir()

        with (
            mock.patch("paths.is_dev_mode", return_value=False),
            mock.patch("cli.fetch_latest_tag", return_value="0.18.0"),
            mock.patch("cli.get_version", return_value="0.17.0"),
            mock.patch("cli.download_and_extract") as mock_download,
            mock.patch("cli.atomic_symlink") as mock_symlink,
            mock.patch("paths.merlin_home", return_value=tmp_path),
            mock.patch("paths.app_dir", return_value=tmp_path),
        ):
            resp = client.post("/api/update")

        data = resp.json()
        assert data["ok"] is True
        assert data["version"] == "0.18.0"
        mock_download.assert_called_once()
        mock_symlink.assert_called_once()

    def test_skips_download_if_version_exists(self, client, tmp_path):
        """POST /api/update skips download if version dir already exists."""
        versions_dir = tmp_path / "versions"
        versions_dir.mkdir()
        version_dir = versions_dir / "0.18.0"
        version_dir.mkdir()  # already downloaded

        with (
            mock.patch("paths.is_dev_mode", return_value=False),
            mock.patch("cli.fetch_latest_tag", return_value="0.18.0"),
            mock.patch("cli.get_version", return_value="0.17.0"),
            mock.patch("cli.download_and_extract") as mock_download,
            mock.patch("cli.atomic_symlink"),
            mock.patch("paths.merlin_home", return_value=tmp_path),
            mock.patch("paths.app_dir", return_value=tmp_path),
        ):
            resp = client.post("/api/update")

        assert resp.json()["ok"] is True
        mock_download.assert_not_called()


class TestSettingsPageUpdateSection:
    def test_settings_page_has_update_section(self, client):
        """Settings page includes the update section HTML."""
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert 'id="update-section"' in resp.text
        assert "Version" in resp.text
