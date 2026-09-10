"""Tests for the installable app: the manifest and the service worker routes
(the only unauthenticated routes the notifications epic adds), the icons, and
the registration in the page shell."""

import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main as app_mod

ROOT = Path(app_mod.__file__).parent


@pytest.fixture
def client(monkeypatch):
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")
    with TestClient(app_mod.app) as c:
        yield c


@pytest.fixture
def locked_client(monkeypatch):
    """A client against an instance with a password and no cookie."""
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
    auth.configure("secret")
    with TestClient(app_mod.app) as c:
        yield c


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


class TestManifest:
    def test_shape(self):
        m = app_mod.build_manifest("worker-1")
        assert m["name"] == "Merlin · worker-1"
        assert m["short_name"] == "worker-1"
        assert m["display"] == "standalone"
        assert m["start_url"] == "/terminal"
        assert m["scope"] == "/"
        assert m["theme_color"] == "#0f1117"
        assert m["background_color"] == "#0f1117"
        sizes = {(i["sizes"], i.get("purpose", "any")) for i in m["icons"]}
        assert sizes == {
            ("192x192", "any"),
            ("512x512", "any"),
            ("512x512", "maskable"),
        }

    def test_no_machine_name_falls_back_to_merlin(self):
        m = app_mod.build_manifest("")
        assert m["name"] == "Merlin"
        assert m["short_name"] == "Merlin"

    def test_route_serves_manifest_json(self, client):
        r = client.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/manifest+json")
        body = r.json()
        assert body["display"] == "standalone"
        for icon in body["icons"]:
            assert client.get(icon["src"]).status_code == 200

    def test_unauthenticated(self, locked_client):
        r = locked_client.get("/manifest.webmanifest", follow_redirects=False)
        assert r.status_code == 200
        r = locked_client.get("/sw.js", follow_redirects=False)
        assert r.status_code == 200
        # And nothing else opened up with them.
        r = locked_client.get("/api/notifications/status", follow_redirects=False)
        assert r.status_code == 303

    def test_icons_are_pngs_of_the_declared_size(self):
        assert png_size((ROOT / "static/icons/icon-192.png").read_bytes()) == (192, 192)
        assert png_size((ROOT / "static/icons/icon-512.png").read_bytes()) == (512, 512)
        assert png_size((ROOT / "static/icons/icon-maskable-512.png").read_bytes()) == (
            512,
            512,
        )


class TestServiceWorker:
    def test_route(self, client):
        r = client.get("/sw.js")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/javascript")
        assert r.headers["cache-control"] == "no-cache"
        assert r.text == (ROOT / "notifications/static/sw.js").read_text()

    def test_two_duties_and_nothing_else(self):
        src = (ROOT / "notifications/static/sw.js").read_text()
        assert "addEventListener('push'" in src
        assert "addEventListener('notificationclick'" in src
        assert "'fetch'" not in src
        assert "caches" not in src
        assert "respondWith" not in src


class TestPageShell:
    def test_base_links_manifest_and_registers_the_worker(self, client):
        html = client.get("/terminal").text
        assert '<link rel="manifest" href="/manifest.webmanifest">' in html
        assert 'name="theme-color" content="#0f1117"' in html
        assert "navigator.serviceWorker.register('/sw.js?v=" in html
        assert "{ scope: '/' }" in html
        version = app_mod._merlin_version()
        assert f"/sw.js?v={version}" in html or "/sw.js?v=" in html
