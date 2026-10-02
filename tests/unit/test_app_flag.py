"""The Apps extension does not exist unless MERLIN_FEATURES turns it on.

Flag off: no registry entry, no nav item, no page, no CLI namespace, no help
entry, no skills, no terminal button. Flag on (environment or config.env):
all of them present. The `app` id stays reserved either way.
"""

import pytest
from fastapi.testclient import TestClient

import ext_commands
import features
import main as app_mod
import paths


@pytest.fixture(autouse=True)
def _no_ambient_flag(monkeypatch):
    monkeypatch.delenv("MERLIN_FEATURES", raising=False)


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


def _write_config(text: str) -> None:
    config = paths.config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text)


class TestFeatures:
    def test_off_by_default(self):
        assert not features.enabled("app")
        assert features.active() == set()

    def test_from_environment(self, monkeypatch):
        monkeypatch.setenv("MERLIN_FEATURES", "app")
        assert features.enabled("app")

    def test_from_config_env(self):
        _write_config("DASHBOARD_PASS=x\nMERLIN_FEATURES=app\n")
        assert features.enabled("app")

    def test_environment_wins_over_config(self, monkeypatch):
        _write_config("MERLIN_FEATURES=app\n")
        monkeypatch.setenv("MERLIN_FEATURES", "")
        assert not features.enabled("app")

    def test_separators_and_case(self, monkeypatch):
        monkeypatch.setenv("MERLIN_FEATURES", " Foo, APP  bar ")
        assert features.active() == {"foo", "app", "bar"}
        assert features.enabled("app")
        assert features.enabled("FOO")
        assert not features.enabled("ap")


class TestCliSurfaces:
    def test_flag_off_hides_everything(self):
        assert "app" not in ext_commands.builtin_extension_dirs()
        assert "app" not in ext_commands.all_extension_states()
        assert "app" not in ext_commands.enabled_extension_source_dirs()
        assert "merlin app" not in ext_commands.format_extension_help()

    def test_flag_off_dispatch_is_unknown_command(self, capsys):
        with pytest.raises(SystemExit) as exc:
            ext_commands.dispatch(["app", "list"])
        assert exc.value.code == 2
        assert "Unknown command: 'app'" in capsys.readouterr().err

    def test_flag_on_exposes_the_extension(self, monkeypatch):
        monkeypatch.setenv("MERLIN_FEATURES", "app")
        dirs = ext_commands.builtin_extension_dirs()
        assert dirs["app"] == paths.app_dir() / "app"
        assert ext_commands.all_extension_states()["app"][1] is True
        assert "app" in ext_commands.enabled_extension_source_dirs()

    def test_flag_on_respects_extensions_toggle(self, monkeypatch):
        monkeypatch.setenv("MERLIN_FEATURES", "app")
        state = paths.extensions_state_path()
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"app": false}')
        assert ext_commands.all_extension_states()["app"][1] is False

    @pytest.mark.parametrize("flag", ["", "app"])
    def test_name_always_reserved(self, monkeypatch, flag):
        monkeypatch.setenv("MERLIN_FEATURES", flag)
        assert "app" in ext_commands.reserved_names()


class TestServer:
    def test_flag_off_at_import(self):
        # The suite imports main with no flag set (isolated MERLIN_HOME).
        assert "app" not in app_mod.extension_registry
        assert not any(i.get("url") == "/apps" for i in app_mod.nav_items)

    def test_flag_off_terminal_has_no_app_button(self, monkeypatch):
        monkeypatch.setattr(app_mod, "TMUX_AVAILABLE", True)
        with TestClient(app_mod.app) as client:
            html = client.get("/terminal").text
        assert 'id="app-btn"' not in html
        assert 'id="app-panel"' not in html

    def test_flag_on_loads_page_nav_and_terminal_button(self, monkeypatch):
        monkeypatch.setenv("MERLIN_FEATURES", "app")
        monkeypatch.setattr(app_mod, "TMUX_AVAILABLE", True)
        routes = list(app_mod.app.router.routes)
        nav = list(app_mod.nav_items)
        try:
            app_mod._load_flagged_builtins()
            info = app_mod.extension_registry["app"]
            assert info.loaded and info.tier == "built-in"
            assert any(i.get("url") == "/apps" for i in app_mod.nav_items)
            with TestClient(app_mod.app) as client:
                assert client.get("/apps").status_code == 200
                html = client.get("/terminal").text
            assert 'id="app-btn"' in html
            assert 'id="app-panel"' in html
        finally:
            app_mod.extension_registry.pop("app", None)
            app_mod.nav_items[:] = nav
            app_mod.app.router.routes[:] = routes

    def test_installed_extension_named_app_rejected(self, monkeypatch):
        ext_dir = paths.extensions_dir() / "app"
        ext_dir.mkdir(parents=True)
        (ext_dir / "app.py").write_text("page_router = None\n")
        try:
            app_mod._load_installed_extensions()
            info = app_mod.extension_registry["app"]
            assert info.tier == "installed" and not info.loaded
            assert "reserved" in (info.error or "")
        finally:
            app_mod.extension_registry.pop("app", None)


def test_flag_on_routes_require_auth(monkeypatch):
    """Through the real app (not a bare router): no cookie, no access."""
    import auth
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("MERLIN_FEATURES", "app")
    routes = list(app_mod.app.router.routes)
    nav = list(app_mod.nav_items)
    try:
        app_mod._load_flagged_builtins()
        auth.configure("secret")
        monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
        with TestClient(app_mod.app) as client:
            for path in (
                "/apps",
                "/apps/probe/play",
                "/api/apps/sessions",
                "/api/apps/saved",
            ):
                response = client.get(path, follow_redirects=False)
                assert response.status_code in (303, 401), (path, response.status_code)
            assert client.post(
                "/api/apps/sessions", json={"argv": ["true"]}, follow_redirects=False
            ).status_code in (303, 401)
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect("/ws/apps/probe/stream") as ws:
                    ws.receive_text()
            assert exc.value.code == 4401
    finally:
        auth.configure("")
        app_mod.extension_registry.pop("app", None)
        app_mod.nav_items[:] = nav
        app_mod.app.router.routes[:] = routes
