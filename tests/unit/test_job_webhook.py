"""Tests for job/webhook.py — the job-source webhook handler."""

import asyncio
import json
import threading
import uuid
from dataclasses import dataclass

import pytest

pytest.importorskip("croniter")

from fastapi.testclient import TestClient

import auth
import main as app_mod
from job import state as job_state
from job import webhook


@pytest.fixture(autouse=True)
def _isolated_job_dirs(tmp_path):
    """Patch job.manage and job.state to use temp directories."""
    from job import manage as job_manage

    orig = {
        "manage_dir": job_manage.JOBS_DIR,
        "state_dir": job_state.JOBS_DIR,
        "state_state_dir": job_state.STATE_DIR,
        "state_locks_dir": job_state.LOCKS_DIR,
        "state_history_file": job_state.HISTORY_FILE,
    }

    jobs_dir_tmp = tmp_path / "jobs"
    jobs_dir_tmp.mkdir()

    job_manage.JOBS_DIR = jobs_dir_tmp
    job_state.JOBS_DIR = jobs_dir_tmp
    job_state.STATE_DIR = jobs_dir_tmp / ".state"
    job_state.LOCKS_DIR = jobs_dir_tmp / ".locks"
    job_state.HISTORY_FILE = jobs_dir_tmp / ".history.json"

    webhook._in_flight.clear()

    yield jobs_dir_tmp

    job_manage.JOBS_DIR = orig["manage_dir"]
    job_state.JOBS_DIR = orig["state_dir"]
    job_state.STATE_DIR = orig["state_state_dir"]
    job_state.LOCKS_DIR = orig["state_locks_dir"]
    job_state.HISTORY_FILE = orig["state_history_file"]
    webhook._in_flight.clear()


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


@pytest.fixture
def client():
    with TestClient(app_mod.app) as c:
        yield c


def _write_job(jobs_dir, job_id="hook-job", **overrides):
    data = {
        "description": "test",
        "type": "command",
        "command": "echo hi",
        "enabled": True,
        "webhook": {"secret": "whk_secret"},
    }
    data.update(overrides)
    data = {k: v for k, v in data.items() if v is not None}
    (jobs_dir / f"{job_id}.json").write_text(json.dumps(data))
    return data


@dataclass
class _FakeResult:
    exit_code: int = 0
    duration: float = 0.1
    result: str = "ok"
    stderr: str = ""
    cost_usd: float | None = None
    session_id: str | None = None


class TestGenerateSecret:
    def test_prefix_and_entropy(self):
        s1 = webhook.generate_secret()
        s2 = webhook.generate_secret()
        assert s1.startswith("whk_")
        assert s1 != s2
        assert len(s1) > 30


class TestResolve:
    def test_unknown_job_resolves_to_none(self, _isolated_job_dirs):
        assert webhook.resolve("missing") is None

    def test_job_without_webhook_resolves_to_none(self, _isolated_job_dirs):
        _write_job(_isolated_job_dirs, webhook=None)
        assert webhook.resolve("hook-job") is None

    def test_job_with_webhook_resolves(self, _isolated_job_dirs):
        _write_job(_isolated_job_dirs)
        target = webhook.resolve("hook-job")
        assert target is not None
        assert target.secret == "whk_secret"
        assert target.enabled is True

    def test_disabled_job_resolves_disabled(self, _isolated_job_dirs):
        _write_job(_isolated_job_dirs, enabled=False)
        target = webhook.resolve("hook-job")
        assert target is not None
        assert target.enabled is False


class TestSingleFlight:
    def test_concurrent_fires_coalesce(self, _isolated_job_dirs, monkeypatch):
        """A fire while a run is in flight starts nothing and reports the
        active run's id; after completion the next fire launches again."""
        from job import runner

        started = threading.Event()
        release = threading.Event()
        executed = []

        def fake_execute(job_id, jobdata, **kwargs):
            executed.append(kwargs.get("request_id"))
            started.set()
            release.wait(timeout=5)
            return _FakeResult()

        monkeypatch.setattr(runner, "_execute_job", fake_execute)
        monkeypatch.setattr(webhook, "_notify", lambda *a, **k: None)
        job = _write_job(_isolated_job_dirs)

        async def _test():
            r1 = webhook._fire("hook-job", job)
            assert r1.status == "launched"
            assert r1.run_id is not None

            await asyncio.to_thread(started.wait, 5)

            r2 = webhook._fire("hook-job", job)
            assert r2.status == "coalesced"
            assert r2.run_id == r1.run_id

            release.set()
            for _ in range(100):
                if "hook-job" not in webhook._in_flight:
                    break
                await asyncio.sleep(0.02)

            r3 = webhook._fire("hook-job", job)
            assert r3.status == "launched"
            assert r3.run_id != r1.run_id
            while "hook-job" in webhook._in_flight:
                await asyncio.sleep(0.02)

        asyncio.run(_test())
        # Exactly the two launched runs executed, with their run ids threaded
        # through as the execution request_id.
        assert len(executed) == 2

    def test_flock_held_elsewhere_coalesces(self, _isolated_job_dirs):
        """A scheduled/manual run (per-job flock held) coalesces the fire."""
        job = _write_job(_isolated_job_dirs)
        lock = job_state.acquire_job_lock("hook-job")
        try:

            async def _test():
                return webhook._fire("hook-job", job)

            result = asyncio.run(_test())
            assert result.status == "coalesced"
            assert result.run_id is None
        finally:
            job_state.release_job_lock(lock)


class TestFreshSession:
    def test_webhook_trigger_forces_fresh_session(self, monkeypatch):
        """Even a non-ephemeral job gets a fresh UUID4 session per webhook
        fire — independent incidents must not share context."""
        from job import runner

        captured = {}

        def fake_invoke(prompt, **kwargs):
            captured["session_id"] = kwargs.get("session_id")
            captured["prompt"] = prompt
            return _FakeResult(session_id=kwargs.get("session_id"))

        monkeypatch.setattr(runner, "invoke", fake_invoke)
        job = {"type": "prompt", "prompt": "triage", "ephemeral": False}

        runner._run_agent("hook-job", job, "req-1", trigger="webhook")

        deterministic = runner.session_id_for_job("hook-job")
        assert captured["session_id"] != deterministic
        uuid.UUID(captured["session_id"])  # valid UUID

    def test_webhook_prompt_marker(self, monkeypatch):
        from job import runner

        captured = {}

        def fake_invoke(prompt, **kwargs):
            captured["prompt"] = prompt
            return _FakeResult()

        monkeypatch.setattr(runner, "invoke", fake_invoke)
        job = {"type": "prompt", "prompt": "triage the outage"}

        runner._run_agent("hook-job", job, "req-1", trigger="webhook")
        assert captured["prompt"].startswith("[Triggered by webhook: hook-job]")

        runner._run_agent("hook-job", job, "req-2")
        assert captured["prompt"].startswith("[Job: hook-job]")


class TestTriggerRecorded:
    def test_history_and_log_carry_trigger(self, _isolated_job_dirs, monkeypatch):
        """A webhook-triggered execution records trigger=webhook in history
        and the run log, under the run_id it was accepted with."""
        from job import logs as job_logs
        from job import runner

        log_dir = _isolated_job_dirs / "run-logs"
        monkeypatch.setattr(job_logs, "_logs_base_dir", lambda: log_dir)

        job = _write_job(_isolated_job_dirs, command="echo webhook-run")
        result = runner._execute_job(
            "hook-job", job, emit_result=False, trigger="webhook", request_id="run-42"
        )
        assert result.exit_code == 0

        history = job_state.get_history("hook-job")
        assert history[0]["trigger"] == "webhook"

        entries = job_logs.list_logs("hook-job")
        assert entries[0]["trigger"] == "webhook"

    def test_dispatcher_marks_schedule(self, _isolated_job_dirs, monkeypatch):
        from job import runner

        job = _write_job(_isolated_job_dirs, command="echo scheduled")
        runner.run_job("hook-job", job, emit_result=False, trigger="schedule")
        history = job_state.get_history("hook-job")
        assert history[0]["trigger"] == "schedule"


@pytest.fixture(autouse=True)
def _reset_whoami_memo():
    """Isolate the whoami in-process memo between tests."""
    webhook._whoami_host = None
    webhook._whoami_at = None
    yield
    webhook._whoami_host = None
    webhook._whoami_at = None


def _fake_whoami(monkeypatch, public_host="wizard.merlincloud.dev"):
    """Fake the portal whoami endpoint; returns the list of requested URLs."""
    import io
    import json as json_mod

    calls: list[str] = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        body = json_mod.dumps({"slug": "wizard", "public_host": public_host}).encode()

        class _Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


class TestWhoamiResolution:
    def test_saas_token_resolves_via_portal(self, monkeypatch):
        monkeypatch.delenv("MERLIN_DASHBOARD_URL", raising=False)
        monkeypatch.delenv("MERLIN_ENVIRONMENT_SLUG", raising=False)
        monkeypatch.setenv("MERLIN_SAAS_TOKEN", "mrl_test")
        monkeypatch.delenv("MERLIN_SAAS_API", raising=False)
        calls = _fake_whoami(monkeypatch)

        assert (
            webhook.public_url("my-job")
            == "https://wizard.merlincloud.dev/webhooks/job/my-job"
        )
        assert calls == ["https://merlincloud.dev/api/instance/whoami"]
        assert webhook.resolve_public_base()[1] == "saas"

    def test_memo_makes_one_call_per_ttl_window(self, monkeypatch):
        monkeypatch.delenv("MERLIN_DASHBOARD_URL", raising=False)
        monkeypatch.setenv("MERLIN_SAAS_TOKEN", "mrl_test")
        calls = _fake_whoami(monkeypatch)

        webhook.public_url("a")
        webhook.public_url("b")
        webhook.public_url("c")
        assert len(calls) == 1

    def test_no_token_makes_no_call(self, monkeypatch):
        monkeypatch.delenv("MERLIN_DASHBOARD_URL", raising=False)
        monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
        monkeypatch.delenv("MERLIN_ENVIRONMENT_SLUG", raising=False)
        calls = _fake_whoami(monkeypatch)

        webhook.public_url("j")
        assert calls == []

    def test_dashboard_url_still_wins_over_whoami(self, monkeypatch):
        monkeypatch.setenv("MERLIN_DASHBOARD_URL", "https://me.example.com")
        monkeypatch.setenv("MERLIN_SAAS_TOKEN", "mrl_test")
        calls = _fake_whoami(monkeypatch)

        assert webhook.public_url("j") == "https://me.example.com/webhooks/job/j"
        assert calls == []

    def test_portal_error_falls_back_to_slug_env(self, monkeypatch):
        import urllib.error

        monkeypatch.delenv("MERLIN_DASHBOARD_URL", raising=False)
        monkeypatch.setenv("MERLIN_SAAS_TOKEN", "mrl_test")
        monkeypatch.setenv("MERLIN_ENVIRONMENT_SLUG", "backup-slug")
        monkeypatch.delenv("MERLIN_SAAS_API", raising=False)

        def failing_urlopen(req, timeout=0):
            raise urllib.error.URLError("portal down")

        monkeypatch.setattr("urllib.request.urlopen", failing_urlopen)
        assert (
            webhook.public_url("j")
            == "https://backup-slug.merlincloud.dev/webhooks/job/j"
        )

    def test_stale_host_survives_portal_outage(self, monkeypatch):
        """After one success, a portal outage keeps the last known host
        instead of downgrading to the IP fallback."""
        import urllib.error

        monkeypatch.delenv("MERLIN_DASHBOARD_URL", raising=False)
        monkeypatch.delenv("MERLIN_ENVIRONMENT_SLUG", raising=False)
        monkeypatch.setenv("MERLIN_SAAS_TOKEN", "mrl_test")
        _fake_whoami(monkeypatch)
        assert "wizard.merlincloud.dev" in webhook.public_url("j")

        def failing_urlopen(req, timeout=0):
            raise urllib.error.URLError("portal down")

        monkeypatch.setattr("urllib.request.urlopen", failing_urlopen)
        webhook._whoami_at = None  # expire the memo, forcing a re-fetch
        assert "wizard.merlincloud.dev" in webhook.public_url("j")

    def test_rename_picked_up_after_ttl(self, monkeypatch):
        monkeypatch.delenv("MERLIN_DASHBOARD_URL", raising=False)
        monkeypatch.setenv("MERLIN_SAAS_TOKEN", "mrl_test")
        _fake_whoami(monkeypatch, public_host="before.merlincloud.dev")
        assert "before.merlincloud.dev" in webhook.public_url("j")

        _fake_whoami(monkeypatch, public_host="after.merlincloud.dev")
        webhook._whoami_at = None  # expire the memo (TTL elapsed)
        assert "after.merlincloud.dev" in webhook.public_url("j")


class TestPublicUrl:
    def test_dashboard_url_wins(self, monkeypatch):
        monkeypatch.setenv("MERLIN_DASHBOARD_URL", "https://merlin.merlincloud.dev")
        monkeypatch.setenv("MERLIN_ENVIRONMENT_SLUG", "other")
        assert (
            webhook.public_url("my-job")
            == "https://merlin.merlincloud.dev/webhooks/job/my-job"
        )
        assert webhook.resolve_public_base() == (
            "https://merlin.merlincloud.dev",
            "override",
        )

    def test_dashboard_url_schemeless_normalized(self, monkeypatch):
        monkeypatch.setenv("MERLIN_DASHBOARD_URL", "example.com:8443")
        assert webhook.public_url("j") == "http://example.com:8443/webhooks/job/j"

    def test_slug_builds_saas_subdomain(self, monkeypatch):
        monkeypatch.delenv("MERLIN_DASHBOARD_URL", raising=False)
        monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
        monkeypatch.setenv("MERLIN_ENVIRONMENT_SLUG", "wizard")
        monkeypatch.delenv("MERLIN_SAAS_API", raising=False)
        assert (
            webhook.public_url("my-job")
            == "https://wizard.merlincloud.dev/webhooks/job/my-job"
        )
        assert webhook.resolve_public_base()[1] == "slug"

    def test_ip_fallback_uses_port(self, monkeypatch):
        monkeypatch.delenv("MERLIN_DASHBOARD_URL", raising=False)
        monkeypatch.delenv("MERLIN_ENVIRONMENT_SLUG", raising=False)
        monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
        monkeypatch.setenv("MERLIN_PORT", "3199")
        monkeypatch.setattr(webhook, "_local_ip", lambda: "192.168.1.50")
        assert webhook.public_url("j") == "http://192.168.1.50:3199/webhooks/job/j"
        assert webhook.resolve_public_base()[1] == "ip"


class TestWebhookManagementApi:
    def test_add_webhook_generates_secret(self, client, _isolated_job_dirs):
        _write_job(_isolated_job_dirs, webhook=None)
        resp = client.post("/api/job/jobs/hook-job/webhook")
        assert resp.status_code == 200
        data = resp.json()
        assert data["webhook"]["secret"].startswith("whk_")
        assert data["webhook_url"].endswith("/webhooks/job/hook-job")

        stored = json.loads((_isolated_job_dirs / "hook-job.json").read_text())
        assert stored["webhook"]["secret"] == data["webhook"]["secret"]

    def test_add_webhook_is_idempotent(self, client, _isolated_job_dirs):
        _write_job(_isolated_job_dirs)
        resp = client.post("/api/job/jobs/hook-job/webhook")
        assert resp.status_code == 200
        assert resp.json()["webhook"]["secret"] == "whk_secret"

    def test_add_webhook_unknown_job_404(self, client, _isolated_job_dirs):
        assert client.post("/api/job/jobs/missing/webhook").status_code == 404

    def test_rotate_replaces_secret(self, client, _isolated_job_dirs):
        _write_job(_isolated_job_dirs)
        resp = client.post("/api/job/jobs/hook-job/webhook/rotate")
        assert resp.status_code == 200
        new_secret = resp.json()["webhook"]["secret"]
        assert new_secret != "whk_secret"
        assert new_secret.startswith("whk_")

    def test_rotate_without_webhook_404(self, client, _isolated_job_dirs):
        _write_job(_isolated_job_dirs, webhook=None)
        assert client.post("/api/job/jobs/hook-job/webhook/rotate").status_code == 404

    def test_delete_removes_block(self, client, _isolated_job_dirs):
        _write_job(_isolated_job_dirs)
        resp = client.delete("/api/job/jobs/hook-job/webhook")
        assert resp.status_code == 204
        stored = json.loads((_isolated_job_dirs / "hook-job.json").read_text())
        assert "webhook" not in stored

    def test_get_job_includes_webhook_url(self, client, _isolated_job_dirs):
        _write_job(_isolated_job_dirs)
        resp = client.get("/api/job/jobs/hook-job")
        assert resp.status_code == 200
        data = resp.json()
        assert data["webhook_url"].endswith("/webhooks/job/hook-job")
        assert data["webhook_url_source"] in ("override", "saas", "slug", "ip")

    def test_last_run_display_uses_history_not_cursor(self, client, _isolated_job_dirs):
        """A webhook run doesn't touch the schedule cursor, but the job's
        'last_run' still reflects it (from history) so the card shows it."""
        _write_job(_isolated_job_dirs, webhook=None)
        # No cursor (get_last_run None), but a run is in history.
        job_state.append_history(
            "hook-job", exit_code=0, duration=1.0, trigger="webhook"
        )
        data = client.get("/api/job/jobs/hook-job").json()
        assert data["last_run"] is not None

    def test_get_job_source_reflects_override(
        self, client, _isolated_job_dirs, monkeypatch
    ):
        monkeypatch.setenv("MERLIN_DASHBOARD_URL", "https://me.example.com")
        _write_job(_isolated_job_dirs)
        data = client.get("/api/job/jobs/hook-job").json()
        assert data["webhook_url_source"] == "override"
        assert data["webhook_url"] == "https://me.example.com/webhooks/job/hook-job"

    def test_get_job_without_webhook_has_no_url(self, client, _isolated_job_dirs):
        _write_job(_isolated_job_dirs, webhook=None)
        resp = client.get("/api/job/jobs/hook-job")
        assert resp.status_code == 200
        assert "webhook_url" not in resp.json()


class TestEndToEndDesk:
    """Through the real front desk: POST /webhooks/job/{id}."""

    def test_fire_launches_run(self, client, _isolated_job_dirs, monkeypatch):
        from job import runner

        ran = threading.Event()

        def fake_execute(job_id, jobdata, **kwargs):
            ran.set()
            return _FakeResult()

        monkeypatch.setattr(runner, "_execute_job", fake_execute)
        monkeypatch.setattr(webhook, "_notify", lambda *a, **k: None)
        _write_job(_isolated_job_dirs)

        resp = client.post(
            "/webhooks/job/hook-job",
            headers={"X-Merlin-Webhook-Secret": "whk_secret"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["status"] == "launched"
        assert body["run_id"]
        assert ran.wait(timeout=5)

    def test_wrong_secret_rejected(self, client, _isolated_job_dirs):
        _write_job(_isolated_job_dirs)
        resp = client.post(
            "/webhooks/job/hook-job",
            headers={"X-Merlin-Webhook-Secret": "wrong"},
        )
        assert resp.status_code == 401

    def test_disabled_job_403(self, client, _isolated_job_dirs):
        _write_job(_isolated_job_dirs, enabled=False)
        resp = client.post(
            "/webhooks/job/hook-job",
            headers={"X-Merlin-Webhook-Secret": "whk_secret"},
        )
        assert resp.status_code == 403

    def test_job_without_webhook_404(self, client, _isolated_job_dirs):
        _write_job(_isolated_job_dirs, webhook=None)
        resp = client.post(
            "/webhooks/job/hook-job",
            headers={"X-Merlin-Webhook-Secret": "whk_secret"},
        )
        assert resp.status_code == 404
