"""Tests for the sandboxed HTML preview: tokens, scope, headers, shim, mint API."""

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from files import html_view
from files.html_view import (
    STORAGE_SHIM,
    TOKEN_TTL_SECONDS,
    inject_shim,
    make_token,
    resolve_in_root,
    verify_token,
)
from files.routes import api_router


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(api_router, prefix="/api/files")
    html_view.register_routes(app)
    return TestClient(app)


@pytest.fixture()
def site(tmp_path):
    """tmp_path/
    site/
      index.html, app.js, css/style.css, .env, .git/config
    outside.txt
    """
    site = tmp_path / "site"
    (site / "css").mkdir(parents=True)
    (site / ".git").mkdir()
    (site / "index.html").write_text(
        "<!doctype html><html><head><title>t</title></head><body>hi</body></html>"
    )
    (site / "app.js").write_text("console.log(1)")
    (site / "css" / "style.css").write_text("body{}")
    (site / ".env").write_text("SECRET=1")
    (site / ".git" / "config").write_text("[core]")
    (tmp_path / "outside.txt").write_text("outside")
    return site


def _url(client, path: Path) -> str:
    resp = client.get("/api/files/view-url", params={"path": str(path)})
    assert resp.status_code == 200, resp.text
    return resp.json()["url"]


class TestToken:
    def test_round_trip(self, tmp_path):
        assert verify_token(make_token(tmp_path)) == tmp_path

    def test_unicode_and_spaces_in_root(self, tmp_path):
        root = tmp_path / "mes modèles"
        assert verify_token(make_token(root)) == root

    def test_expired(self, tmp_path):
        token = make_token(tmp_path, now=1000)
        assert verify_token(token, now=1000 + TOKEN_TTL_SECONDS - 1) == tmp_path
        assert verify_token(token, now=1000 + TOKEN_TTL_SECONDS + 1) is None

    def test_tampered_root(self, tmp_path):
        other = make_token(Path("/"))
        encoded, expiry, sig = make_token(tmp_path).split(".")
        forged = f"{other.split('.')[0]}.{expiry}.{sig}"
        assert verify_token(forged) is None

    def test_tampered_expiry(self, tmp_path):
        encoded, expiry, sig = make_token(tmp_path).split(".")
        assert verify_token(f"{encoded}.{int(expiry) + 999}.{sig}") is None

    @pytest.mark.parametrize("token", ["", "abc", "a.b", "a.b.c.d", "a.notanint.sig"])
    def test_malformed(self, token):
        assert verify_token(token) is None


class TestScope:
    def test_file_in_root(self, site):
        assert resolve_in_root(site, "index.html") == (site / "index.html").resolve()

    def test_subfolder(self, site):
        assert resolve_in_root(site, "css/style.css") is not None

    @pytest.mark.parametrize("rel", [".env", ".git/config", "css/.hidden"])
    def test_hidden_segments_refused(self, site, rel):
        assert resolve_in_root(site, rel) is None

    def test_parent_traversal_refused(self, site):
        assert resolve_in_root(site, "../outside.txt") is None

    def test_symlink_escape_refused(self, site, tmp_path):
        os.symlink(tmp_path / "outside.txt", site / "link.txt")
        assert resolve_in_root(site, "link.txt") is None

    def test_symlink_inside_allowed(self, site):
        os.symlink(site / "app.js", site / "alias.js")
        assert resolve_in_root(site, "alias.js") is not None

    def test_directory_and_missing_refused(self, site):
        assert resolve_in_root(site, "css") is None
        assert resolve_in_root(site, "nope.js") is None
        assert resolve_in_root(site, "") is None

    def test_blocked_prefix_refused(self):
        assert resolve_in_root(Path("/proc"), "self/status") is None


class TestShim:
    def test_after_head(self):
        out = inject_shim(b"<!DOCTYPE html><html><HEAD lang='x'><script>x</script>")
        assert out.index(STORAGE_SHIM.encode()) == out.index(b"<HEAD lang='x'>") + 15
        assert out.index(STORAGE_SHIM.encode()) < out.index(b"<script>x")

    def test_after_doctype_when_no_head(self):
        out = inject_shim(b"<!doctype html><script>x</script>")
        assert out.startswith(b"<!doctype html>" + STORAGE_SHIM.encode())

    def test_prepended_when_bare(self):
        assert inject_shim(b"<p>hi</p>").startswith(STORAGE_SHIM.encode())

    def test_header_tag_is_not_head(self):
        out = inject_shim(b"<!doctype html><header>x</header>")
        assert out.startswith(b"<!doctype html>" + STORAGE_SHIM.encode())


class TestMint:
    def test_mints_url_named_after_file(self, client, site):
        url = _url(client, site / "index.html")
        assert url.startswith("/files-view/") and url.endswith("/index.html")

    def test_refuses_non_html(self, client, site):
        resp = client.get("/api/files/view-url", params={"path": str(site / "app.js")})
        assert resp.status_code == 400

    def test_refuses_missing_and_directory(self, client, site):
        for p in (site / "nope.html", site):
            resp = client.get("/api/files/view-url", params={"path": str(p)})
            assert resp.status_code == 404

    def test_refuses_hidden_html(self, client, site):
        (site / ".secret.html").write_text("<p>x</p>")
        resp = client.get(
            "/api/files/view-url", params={"path": str(site / ".secret.html")}
        )
        assert resp.status_code == 403


class TestServe:
    def test_html_has_shim_and_sandbox_headers(self, client, site):
        resp = client.get(_url(client, site / "index.html"))
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert STORAGE_SHIM in resp.text
        assert "<title>t</title>" in resp.text
        csp = resp.headers["content-security-policy"]
        assert csp.startswith("sandbox ") and "allow-scripts" in csp
        assert "allow-same-origin" not in csp
        assert "allow-top-navigation" not in csp
        assert resp.headers["access-control-allow-origin"] == "*"
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert resp.headers["cache-control"] == "no-store"

    def test_relative_assets_under_same_token(self, client, site):
        base = _url(client, site / "index.html").rsplit("/", 1)[0]
        js = client.get(f"{base}/app.js")
        assert js.status_code == 200 and js.text == "console.log(1)"
        assert "content-security-policy" in js.headers
        assert client.get(f"{base}/css/style.css").status_code == 200

    def test_hidden_and_outside_are_404(self, client, site):
        base = _url(client, site / "index.html").rsplit("/", 1)[0]
        for rel in (".env", ".git/config", "%2e%2e/outside.txt"):
            assert client.get(f"{base}/{rel}").status_code == 404, rel
        # A literal ".." is collapsed by the URL layer before routing, which
        # lands it on a token that does not verify: refused either way.
        assert client.get(f"{base}/../outside.txt").status_code in (403, 404)

    def test_bad_token_is_403(self, client, site):
        token = make_token(site)
        flipped = token[:-1] + ("1" if token[-1] == "0" else "0")
        resp = client.get(f"/files-view/{flipped}/index.html")
        assert resp.status_code == 403
        assert "expired" in resp.json()["detail"]

    def test_head_request(self, client, site):
        resp = client.head(_url(client, site / "index.html"))
        assert resp.status_code == 200
