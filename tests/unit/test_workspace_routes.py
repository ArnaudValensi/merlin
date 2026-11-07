"""The workspace API (workspace/routes.py) on the real app."""

import pytest
from fastapi.testclient import TestClient

import main as app_mod
from workspace import restore as rs
from workspace import service


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


def test_restore_answers_409_while_another_restore_runs(client, monkeypatch):
    def busy():
        raise rs.RestoreBusy("a restore is already running")

    monkeypatch.setattr(service, "restore_pending", busy)
    r = client.post("/api/workspace/restore")
    assert r.status_code == 409
    assert r.json()["detail"] == "a restore is already running"


def test_restore_answers_503_when_tmux_could_not_be_read(client, monkeypatch):
    def unreadable():
        raise rs.RestoreUnreadable(
            "tmux could not be read, nothing restored: try again"
        )

    monkeypatch.setattr(service, "restore_pending", unreadable)
    r = client.post("/api/workspace/restore")
    assert r.status_code == 503
    assert "try again" in r.json()["detail"]


def test_restore_returns_the_engine_result(client, monkeypatch):
    result = {"restored": ["a"], "skipped": [], "failed": [], "agents": 2}
    monkeypatch.setattr(service, "restore_pending", lambda: result)
    r = client.post("/api/workspace/restore")
    assert r.status_code == 200 and r.json() == result


def test_pending_and_dismiss(client, monkeypatch):
    monkeypatch.setattr(service, "pending_info", lambda: {"pending": False})
    assert client.get("/api/workspace/pending").json() == {"pending": False}
    dismissed = []
    monkeypatch.setattr(service, "dismiss_pending", lambda *a: dismissed.append(a))
    assert client.post("/api/workspace/dismiss").json() == {"ok": True}
    assert dismissed == [()]
