"""Integration tests for the extension system — Phase 5.

Lifecycle tests that exercise the full flow across state management,
registry, API, and config.
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main as app_mod
from main import (
    ExtensionInfo,
    _load_extensions_state,
    _save_extensions_state,
    _resolve_enabled,
    _load_extension,
    _read_config_env,
    _write_config_env,
    extension_registry,
    nav_items,
    BUILT_IN_DEFAULTS,
)


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


class TestExtensionLifecycle:
    def test_disable_extension_removes_nav(self, tmp_path):
        """Disable notes in state → _resolve_enabled returns False → nav wouldn't include it."""
        state = {"notes": False}
        _save_extensions_state(state)
        loaded = _load_extensions_state()
        assert loaded["notes"] is False
        assert _resolve_enabled("notes", "built-in", loaded) is False

    def test_enable_extension_adds_nav(self, tmp_path):
        """Enable notes in state → _resolve_enabled returns True."""
        state = {"notes": True}
        _save_extensions_state(state)
        loaded = _load_extensions_state()
        assert loaded["notes"] is True
        assert _resolve_enabled("notes", "built-in", loaded) is True

    def test_broken_extension_graceful(self, tmp_path):
        """Create broken extension, load → has error, other extensions unaffected."""
        ext_dir = tmp_path / "extensions" / "broken-int"
        ext_dir.mkdir(parents=True)
        (ext_dir / "broken_int.py").write_text("raise ImportError('missing pandas')")

        def loader():
            sys.path.insert(0, str(ext_dir))
            return __import__("broken_int")

        _load_extension("broken-int", "installed", loader)

        info = extension_registry.get("broken-int")
        assert info is not None
        assert info.loaded is False
        assert "missing pandas" in info.error

        # Core extensions still fine
        assert extension_registry["files"].loaded is True
        assert extension_registry["terminal"].loaded is True

        # Cleanup
        sys.path[:] = [p for p in sys.path if str(ext_dir) not in p]
        if "broken_int" in sys.modules:
            del sys.modules["broken_int"]
        extension_registry.pop("broken-int", None)

    def test_missing_metadata_defaults(self, tmp_path):
        """Extension without EXTENSION_META gets name from folder."""
        ext_dir = tmp_path / "extensions" / "cool-tool"
        ext_dir.mkdir(parents=True)
        (ext_dir / "cool_tool.py").write_text(
            "from fastapi import APIRouter\nrouter = APIRouter()\n"
        )

        def loader():
            sys.path.insert(0, str(ext_dir))
            return __import__("cool_tool")

        _load_extension("cool-tool", "installed", loader)

        info = extension_registry.get("cool-tool")
        assert info is not None
        assert info.loaded is True
        assert info.meta.get("name") == "Cool Tool"

        # Cleanup
        sys.path[:] = [p for p in sys.path if str(ext_dir) not in p]
        if "cool_tool" in sys.modules:
            del sys.modules["cool_tool"]
        extension_registry.pop("cool-tool", None)

    def test_extensions_json_missing_on_first_run(self, tmp_path):
        """No extensions.json → all defaults apply correctly."""
        assert not (tmp_path / "extensions.json").exists()
        state = _load_extensions_state()
        assert state == {}
        # Built-in defaults apply
        assert _resolve_enabled("notes", "built-in", state) is True
        assert _resolve_enabled("merlin-bot", "built-in", state) is False
        # Installed defaults to True
        assert _resolve_enabled("video-scenes", "installed", state) is True

    def test_config_env_roundtrip(self, client, tmp_path):
        """Save config via API → read config.env → values match."""
        (tmp_path / "config.env").write_text("")
        client.post("/api/settings", json={"DASHBOARD_PASS": "test-pw"})
        cfg = _read_config_env()
        assert cfg["DASHBOARD_PASS"] == "test-pw"

    def test_toggle_persists_across_loads(self, client, tmp_path):
        """Toggle extension, save state, reload state → toggle persisted."""
        client.post("/api/extensions/notes/toggle")
        loaded = _load_extensions_state()
        assert "notes" in loaded
        # Toggle again
        client.post("/api/extensions/notes/toggle")
        loaded2 = _load_extensions_state()
        assert loaded["notes"] != loaded2["notes"]
