"""Tests for main.mount_module() — the framework-owned module wiring helper.

mount_module() is the single wiring path for both core modules and extensions.
It must:
  - mount api_router at /api/{slug} and page_router at /{slug}, both authed
  - resolve slug from URL_SLUG, defaulting to the module id
  - mount STATIC_DIR at /static/{name}
  - call register_routes(app) as an escape hatch that owns its own auth, and
    log that it did so
  - (transitionally) still mount a legacy plain `router`
"""

from types import SimpleNamespace

import pytest
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

import auth
import main as app_mod


@pytest.fixture(autouse=True)
def _no_saas(monkeypatch):
    """Drop SaaS mode so auth falls back to the password flow, not a redirect
    to merlincloud.dev."""
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)


@pytest.fixture
def client():
    return TestClient(app_mod.app)


def _make_module(**attrs):
    """A stand-in module object exposing only the attributes given."""
    return SimpleNamespace(**attrs)


def _api_router():
    r = APIRouter()

    @r.get("/ping")
    def ping():
        return {"ok": True}

    return r


def _page_router():
    r = APIRouter()

    @r.get("", response_class=PlainTextResponse)
    def index():
        return "index"

    @r.get("/sub", response_class=PlainTextResponse)
    def sub():
        return "sub"

    return r


# ---------------------------------------------------------------------------
# Route resolution
# ---------------------------------------------------------------------------


def test_api_router_mounts_under_api_slug(monkeypatch, client):
    auth.configure("")  # auth off for path-resolution checks
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    app_mod.mount_module(_make_module(api_router=_api_router()), "widgets")

    resp = client.get("/api/widgets/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_page_router_mounts_under_slug(monkeypatch, client):
    auth.configure("")
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    app_mod.mount_module(_make_module(page_router=_page_router()), "gadgets")

    assert client.get("/gadgets").text == "index"
    assert client.get("/gadgets/sub").text == "sub"


def test_url_slug_overrides_module_id(monkeypatch, client):
    auth.configure("")
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    mod = _make_module(URL_SLUG="things", api_router=_api_router())
    app_mod.mount_module(mod, "thing_module")

    assert client.get("/api/things/ping").status_code == 200
    # The module id is NOT used when URL_SLUG is set.
    assert client.get("/api/thing_module/ping").status_code == 404


# ---------------------------------------------------------------------------
# Auth is applied by the framework to api_router / page_router
# ---------------------------------------------------------------------------


def test_auto_mounted_routers_are_authed(monkeypatch, client):
    """With a password set, unauthenticated hits redirect to /login."""
    auth.configure("secret")
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    app_mod.mount_module(
        _make_module(api_router=_api_router(), page_router=_page_router()),
        "authcheck",
    )

    api = client.get("/api/authcheck/ping", follow_redirects=False)
    assert api.status_code == 303
    assert "/login" in api.headers["location"]

    page = client.get("/authcheck", follow_redirects=False)
    assert page.status_code == 303
    assert "/login" in page.headers["location"]


# ---------------------------------------------------------------------------
# register_routes(app) escape hatch
# ---------------------------------------------------------------------------


def test_register_routes_escape_hatch_bypasses_framework_auth(monkeypatch, client):
    """The escape hatch owns its own auth: a route it registers with no auth
    stays reachable even when a password is configured."""
    auth.configure("secret")
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)

    def register_routes(app):
        @app.get("/escape/open", response_class=PlainTextResponse)
        def open_route():
            return "public"

    app_mod.mount_module(_make_module(register_routes=register_routes), "escaper")

    # No framework auth wrapped around it — the module owns the path + auth.
    resp = client.get("/escape/open")
    assert resp.status_code == 200
    assert resp.text == "public"


def test_register_routes_usage_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    auth.configure("")

    def register_routes(app):
        @app.get("/escape/logged")
        def _r():
            return {}

    with caplog.at_level("INFO", logger="merlin"):
        app_mod.mount_module(
            _make_module(register_routes=register_routes), "loudmodule"
        )

    assert any(
        "loudmodule" in rec.message and "register_routes" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# The old plain `router` attribute is no longer part of the contract
# ---------------------------------------------------------------------------


def test_legacy_router_is_ignored_and_warns(monkeypatch, client, caplog):
    """A module exposing only the dropped `router` attribute mounts nothing —
    the framework mounts api_router/page_router, not a plain router — and the
    silent no-op is surfaced as a warning so authors aren't left debugging a
    routeless module."""
    auth.configure("")
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")

    legacy = APIRouter()

    @legacy.get("/legacy/endpoint")
    def _legacy():
        return {"legacy": True}

    with caplog.at_level("WARNING", logger="merlin"):
        app_mod.mount_module(_make_module(router=legacy), "legacymod")

    assert client.get("/legacy/endpoint").status_code == 404
    assert any(
        "legacymod" in rec.message and "legacy `router`" in rec.message
        for rec in caplog.records
    )


def test_no_warning_for_routeless_commands_only_module(monkeypatch, caplog):
    """A commands/skills-only module exports no routers and no legacy `router`
    — that's legitimate, so it must NOT warn."""
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")

    with caplog.at_level("WARNING", logger="merlin"):
        app_mod.mount_module(_make_module(NAV_ITEMS=[]), "commandsonly")

    assert not any("commandsonly" in rec.message for rec in caplog.records)
