"""Tests for the webhooks front desk — the public, secret-gated route."""

import pytest

pytest.importorskip("croniter")

from fastapi.testclient import TestClient

import auth
import main as app_mod
import webhooks


@pytest.fixture(autouse=True)
def _clean_desk(monkeypatch):
    """Isolate the desk's registry per test."""
    saved = dict(webhooks._registry)
    webhooks._registry.clear()

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")

    yield

    webhooks._registry.clear()
    webhooks._registry.update(saved)


@pytest.fixture
def client():
    with TestClient(app_mod.app) as c:
        yield c


def _register(
    source="testsrc",
    target="known-job",
    secret="whk_test",
    enabled=True,
    status="launched",
    run_id="run-1",
):
    """Register a fake resolver; returns the list of fire() call markers."""
    calls: list[str] = []

    def fire() -> webhooks.FireResult:
        calls.append(target)
        return webhooks.FireResult(status=status, run_id=run_id)

    def resolver(target_id: str):
        if target_id != target:
            return None
        return webhooks.WebhookTarget(secret=secret, fire=fire, enabled=enabled)

    webhooks.register(source, resolver)
    return calls


class TestDispatch:
    def test_unknown_source_returns_404(self, client):
        resp = client.post("/webhooks/nope/some-job")
        assert resp.status_code == 404

    def test_invalid_target_id_returns_404(self, client):
        _register()
        resp = client.post(
            "/webhooks/testsrc/..%2Fetc",
            headers={"X-Merlin-Webhook-Secret": "whk_test"},
        )
        assert resp.status_code == 404

    def test_overlong_target_id_returns_404(self, client):
        _register()
        long_id = "a" * 31
        resp = client.post(
            f"/webhooks/testsrc/{long_id}",
            headers={"X-Merlin-Webhook-Secret": "whk_test"},
        )
        assert resp.status_code == 404

    def test_unknown_target_returns_404(self, client):
        _register()
        resp = client.post(
            "/webhooks/testsrc/other-job",
            headers={"X-Merlin-Webhook-Secret": "whk_test"},
        )
        assert resp.status_code == 404

    def test_missing_secret_returns_401(self, client):
        calls = _register()
        resp = client.post("/webhooks/testsrc/known-job")
        assert resp.status_code == 401
        assert calls == []

    def test_wrong_secret_returns_401(self, client):
        calls = _register()
        resp = client.post(
            "/webhooks/testsrc/known-job",
            headers={"X-Merlin-Webhook-Secret": "wrong"},
        )
        assert resp.status_code == 401
        assert calls == []

    def test_valid_secret_header_fires(self, client):
        calls = _register()
        resp = client.post(
            "/webhooks/testsrc/known-job",
            headers={"X-Merlin-Webhook-Secret": "whk_test"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "status": "launched", "run_id": "run-1"}
        assert calls == ["known-job"]

    def test_valid_secret_query_token_fires(self, client):
        calls = _register()
        resp = client.post("/webhooks/testsrc/known-job?token=whk_test")
        assert resp.status_code == 200
        assert calls == ["known-job"]

    def test_disabled_target_returns_403(self, client):
        calls = _register(enabled=False)
        resp = client.post(
            "/webhooks/testsrc/known-job",
            headers={"X-Merlin-Webhook-Secret": "whk_test"},
        )
        assert resp.status_code == 403
        assert calls == []

    def test_disabled_target_with_wrong_secret_returns_401(self, client):
        """The secret is checked before the enabled state: a caller without
        the secret cannot learn whether a target is disabled."""
        _register(enabled=False)
        resp = client.post(
            "/webhooks/testsrc/known-job",
            headers={"X-Merlin-Webhook-Secret": "wrong"},
        )
        assert resp.status_code == 401

    def test_coalesced_status_passthrough(self, client):
        _register(status="coalesced", run_id=None)
        resp = client.post(
            "/webhooks/testsrc/known-job",
            headers={"X-Merlin-Webhook-Secret": "whk_test"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "status": "coalesced", "run_id": None}


class TestPublicMount:
    def test_route_is_public_when_auth_is_on(self, client):
        """/webhooks/* answers directly (no login redirect) even with a
        dashboard password configured; /api stays gated."""
        auth.configure("hunter2")
        _register()

        gated = client.get("/api/jobs/jobs", follow_redirects=False)
        assert gated.status_code == 303

        resp = client.post(
            "/webhooks/testsrc/known-job",
            headers={"X-Merlin-Webhook-Secret": "whk_test"},
            follow_redirects=False,
        )
        assert resp.status_code == 200


class TestNoLockout:
    """The secret is the gate; a valid secret is never blocked by prior
    failures or by a caller-supplied X-Forwarded-For. There is no in-app
    throttle (flood protection is an edge concern)."""

    def _fail(self, client, ip="9.9.9.9"):
        return client.post(
            "/webhooks/testsrc/known-job",
            headers={"X-Merlin-Webhook-Secret": "wrong", "X-Forwarded-For": ip},
        )

    def test_valid_secret_fires_after_many_failures(self, client):
        """A stranger spamming wrong secrets cannot lock out the real sender."""
        calls = _register()
        for _ in range(50):
            assert self._fail(client).status_code == 401
        resp = client.post(
            "/webhooks/testsrc/known-job",
            headers={"X-Merlin-Webhook-Secret": "whk_test"},
        )
        assert resp.status_code == 200
        assert calls == ["known-job"]

    def test_wrong_secret_always_401_never_429(self, client):
        """No throttle: repeated wrong secrets keep returning 401, not 429."""
        _register()
        for _ in range(30):
            resp = self._fail(client)
            assert resp.status_code == 401

    def test_forged_xff_does_not_affect_outcome(self, client):
        """A spoofed X-Forwarded-For (victim's IP) cannot poison anything:
        the victim's valid secret still fires."""
        calls = _register()
        for _ in range(20):
            self._fail(client, ip="203.0.113.5")  # pretend to be the real sender
        resp = client.post(
            "/webhooks/testsrc/known-job",
            headers={
                "X-Merlin-Webhook-Secret": "whk_test",
                "X-Forwarded-For": "203.0.113.5",
            },
        )
        assert resp.status_code == 200
        assert calls == ["known-job"]

    def test_trailing_newline_id_rejected(self, client):
        """fullmatch: a target id with a trailing newline is a 404, not a pass."""
        _register()
        resp = client.post(
            "/webhooks/testsrc/known-job%0A",
            headers={"X-Merlin-Webhook-Secret": "whk_test"},
        )
        assert resp.status_code == 404


class TestRegistry:
    def test_duplicate_source_keeps_first_resolver(self, client):
        calls_first = _register(secret="first-secret")

        def hijack_resolver(target_id: str):
            return webhooks.WebhookTarget(
                secret="hijack", fire=lambda: webhooks.FireResult(status="launched")
            )

        webhooks.register("testsrc", hijack_resolver)

        resp = client.post(
            "/webhooks/testsrc/known-job",
            headers={"X-Merlin-Webhook-Secret": "first-secret"},
        )
        assert resp.status_code == 200
        assert calls_first == ["known-job"]

    def test_loader_registers_extension_webhook_handlers(self):
        """An extension exporting WEBHOOK_HANDLERS is wired into the desk."""
        from types import ModuleType

        from fastapi import APIRouter

        from main import _load_extension, extension_registry

        mod = ModuleType("fake_hook_ext")
        mod.router = APIRouter()
        mod.WEBHOOK_HANDLERS = {"fakesrc": lambda target_id: None}

        _load_extension("fake-hook-ext", "installed", lambda: mod)
        try:
            assert "fakesrc" in webhooks._registry
        finally:
            extension_registry.pop("fake-hook-ext", None)


class TestEventLogging:
    def test_fire_and_rejection_are_logged(self, client):
        from lib.event_log import WebhookRequestEvent, read_events

        _register()
        client.post(
            "/webhooks/testsrc/known-job",
            headers={"X-Merlin-Webhook-Secret": "whk_test"},
        )
        client.post(
            "/webhooks/testsrc/known-job",
            headers={"X-Merlin-Webhook-Secret": "wrong"},
        )

        events = read_events(event_type="webhook_request")
        outcomes = [e.outcome for e in events if isinstance(e, WebhookRequestEvent)]
        assert "launched" in outcomes
        assert "rejected_secret" in outcomes
