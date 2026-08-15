"""Mount, authentication, and fixture-boundary tests for Timeline."""

import pytest
from fastapi.testclient import TestClient

import auth
import main as app_mod
import paths


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("MERLIN_TIMELINE_FIXTURES", raising=False)
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    monkeypatch.setenv("MERLIN_ACTIVITY_HOOKS", "ask")
    auth.configure("")
    with TestClient(app_mod.app) as test_client:
        yield test_client
    auth.configure("")


def test_timeline_page_and_statics_are_mounted(client):
    page = client.get("/timeline")
    assert page.status_code == 200
    assert "Agent activity" in page.text
    assert 'href="/static/timeline/timeline.css"' in page.text
    assert client.get("/static/timeline/timeline.js").status_code == 200


def test_timeline_is_a_built_in_extension_navigation_item(client):
    page = client.get("/timeline")
    assert 'href="/timeline"' in page.text
    assert ">Timeline</span>" in page.text
    info = app_mod.extension_registry["timeline"]
    assert info.tier == "built-in"
    assert info.enabled is True
    assert info.loaded is True


def test_disable_turns_capture_off_before_removing_provider_hooks(monkeypatch):
    import timeline
    from timeline import consent, reconcile

    calls = []
    monkeypatch.setattr(
        consent, "set_capture_mode", lambda mode: calls.append(("mode", mode))
    )
    monkeypatch.setattr(
        reconcile, "remove_hooks", lambda: calls.append(("remove", None))
    )

    timeline.disable()

    assert calls == [("mode", "off"), ("remove", None)]


def test_page_and_api_are_authenticated(client, monkeypatch):
    auth.configure("secret")
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
    assert client.get("/timeline", follow_redirects=False).status_code == 303
    assert client.get("/api/timeline", follow_redirects=False).status_code == 303


def test_url_cannot_enable_fixture_data(client):
    response = client.get("/api/timeline?fixture=1&state=ready")
    assert response.status_code == 200
    assert response.json()["state"] == "collector-disabled"


def test_explicit_process_setting_enables_deterministic_fixture(client, monkeypatch):
    monkeypatch.setenv("MERLIN_TIMELINE_FIXTURES", "1")
    response = client.get("/api/timeline")
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "ready"
    assert data["source"] == "deterministic-fixture"
    assert {item["status"] for item in data["items"]} >= {
        "blocked",
        "error",
        "interrupted",
        "running",
    }


@pytest.mark.parametrize(
    "scenario", ["empty", "disabled", "disconnected", "loading", "no-results"]
)
def test_fixture_has_designed_non_populated_states(client, monkeypatch, scenario):
    monkeypatch.setenv("MERLIN_TIMELINE_FIXTURES", "1")
    response = client.get(f"/api/timeline?state={scenario}")
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_fixture_surfaces_malformed_record_count(client, monkeypatch):
    monkeypatch.setenv("MERLIN_TIMELINE_FIXTURES", "1")
    response = client.get("/api/timeline?state=malformed")
    assert response.status_code == 200
    assert response.json()["anomalies"] == 3


def test_consent_api_is_authenticated_and_explains_privacy(client, monkeypatch):
    monkeypatch.delenv("MERLIN_ACTIVITY_HOOKS", raising=False)
    response = client.get("/api/timeline/consent")
    assert response.status_code == 200
    value = response.json()
    assert value["mode"] == "ask"
    assert any("prompt" in item for item in value["never_stores"])
    auth.configure("secret")
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
    assert (
        client.get("/api/timeline/consent", follow_redirects=False).status_code == 303
    )


def test_consent_api_updates_and_reconciles(client, monkeypatch):
    from timeline import routes

    monkeypatch.setattr(routes, "sync_hooks", lambda: "synced")
    response = client.post("/api/timeline/consent", json={"mode": "auto"})
    assert response.status_code == 200
    assert response.json() == {
        "mode": "auto",
        "source": "config",
        "status": "synced",
    }
    assert "MERLIN_ACTIVITY_HOOKS=auto" in paths.config_path().read_text()
    assert (
        client.post("/api/timeline/consent", json={"mode": "invalid"}).status_code
        == 422
    )
