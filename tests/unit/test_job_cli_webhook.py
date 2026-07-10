"""Tests for the webhook-related job CLI commands: url, webhook, test, add."""

import io
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("croniter")

from job import manage as job_manage


@pytest.fixture(autouse=True)
def temp_jobs_dir(tmp_path):
    """Point job.manage at a temp jobs directory."""
    orig = job_manage.JOBS_DIR
    job_manage.JOBS_DIR = tmp_path / "jobs"
    job_manage.JOBS_DIR.mkdir()
    yield job_manage.JOBS_DIR
    job_manage.JOBS_DIR = orig


def _save(job_id="hook-job", **overrides):
    job = {
        "description": "test",
        "type": "prompt",
        "prompt": "do things",
        "enabled": True,
    }
    job.update(overrides)
    job_manage.save_job(job_id, job)
    return job


def _ns(**kwargs):
    base = {"enable": False, "disable": False, "rotate": False}
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestCmdUrl:
    def test_url_with_webhook(self, monkeypatch):
        _save(webhook={"secret": "whk_s3cret"})
        monkeypatch.setenv("MERLIN_DASHBOARD_URL", "https://me.example.com")
        result = job_manage.cmd_url(_ns(job_id="hook-job"))
        assert result["ok"] is True
        assert result["url"] == "https://me.example.com/webhooks/job/hook-job"
        assert result["secret"] == "whk_s3cret"
        assert "whk_s3cret" in result["curl"]

    def test_url_without_webhook_errors_with_hint(self):
        _save()
        result = job_manage.cmd_url(_ns(job_id="hook-job"))
        assert result["ok"] is False
        assert "webhook" in result["error"]
        assert "--enable" in result["error"]

    def test_url_unknown_job(self):
        result = job_manage.cmd_url(_ns(job_id="missing"))
        assert result["ok"] is False


class TestCmdWebhook:
    def test_enable_generates_secret(self):
        _save()
        result = job_manage.cmd_webhook(_ns(job_id="hook-job", enable=True))
        assert result["ok"] is True
        assert result["secret"].startswith("whk_")

        stored = job_manage.load_job("hook-job")
        assert stored["webhook"]["secret"] == result["secret"]

    def test_enable_is_idempotent(self):
        _save(webhook={"secret": "whk_existing"})
        result = job_manage.cmd_webhook(_ns(job_id="hook-job", enable=True))
        assert result["ok"] is True
        assert result["secret"] == "whk_existing"

    def test_rotate_replaces_secret(self):
        _save(webhook={"secret": "whk_old"})
        result = job_manage.cmd_webhook(_ns(job_id="hook-job", rotate=True))
        assert result["ok"] is True
        assert result["secret"] != "whk_old"
        assert job_manage.load_job("hook-job")["webhook"]["secret"] == result["secret"]

    def test_rotate_without_webhook_errors(self):
        _save()
        result = job_manage.cmd_webhook(_ns(job_id="hook-job", rotate=True))
        assert result["ok"] is False

    def test_disable_removes_block(self):
        _save(webhook={"secret": "whk_x"})
        result = job_manage.cmd_webhook(_ns(job_id="hook-job", disable=True))
        assert result["ok"] is True
        assert "webhook" not in job_manage.load_job("hook-job")

    def test_status_without_flags(self):
        _save(webhook={"secret": "whk_x"})
        result = job_manage.cmd_webhook(_ns(job_id="hook-job"))
        assert result["ok"] is True
        assert result["webhook_enabled"] is True
        assert result["secret"] == "whk_x"

    def test_status_no_hook(self):
        _save()
        result = job_manage.cmd_webhook(_ns(job_id="hook-job"))
        assert result == {
            "ok": True,
            "job_id": "hook-job",
            "webhook_enabled": False,
        }


class TestCmdTest:
    def test_fires_local_server(self, monkeypatch):
        _save(webhook={"secret": "whk_s3cret"})
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["secret"] = req.get_header("X-merlin-webhook-secret")
            body = json.dumps(
                {"ok": True, "status": "launched", "run_id": "r-1"}
            ).encode()

            class _Resp(io.BytesIO):
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return _Resp(body)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = job_manage.cmd_test(_ns(job_id="hook-job", port=3123))
        assert result["ok"] is True
        assert result["status"] == "launched"
        assert result["run_id"] == "r-1"
        assert captured["url"] == "http://127.0.0.1:3123/webhooks/job/hook-job"
        assert captured["method"] == "POST"
        assert captured["secret"] == "whk_s3cret"

    def test_server_unreachable_gives_hint(self, monkeypatch):
        import urllib.error

        _save(webhook={"secret": "whk_s3cret"})

        def fake_urlopen(req, timeout=0):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = job_manage.cmd_test(_ns(job_id="hook-job", port=3199))
        assert result["ok"] is False
        assert "3199" in result["error"]
        assert "running" in result["error"]

    def test_http_error_reported(self, monkeypatch):
        import urllib.error

        _save(webhook={"secret": "whk_s3cret"})

        def fake_urlopen(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = job_manage.cmd_test(_ns(job_id="hook-job", port=3123))
        assert result["ok"] is False
        assert result["http_status"] == 401

    def test_without_webhook_errors(self):
        _save()
        result = job_manage.cmd_test(_ns(job_id="hook-job", port=3123))
        assert result["ok"] is False
        assert "--enable" in result["error"]


class TestCmdAddWebhook:
    def _add_args(self, **overrides):
        args = {
            "id": "new-job",
            "description": "",
            "schedule": None,
            "prompt": "do it",
            "discord_channel": None,
            "report_mode": "always",
            "max_turns": 0,
            "webhook": False,
            "dry_run": False,
        }
        args.update(overrides)
        return SimpleNamespace(**args)

    def test_add_without_schedule(self):
        result = job_manage.cmd_add(self._add_args())
        assert result["ok"] is True
        stored = job_manage.load_job("new-job")
        assert "schedule" not in stored

    def test_add_with_webhook_flag(self):
        result = job_manage.cmd_add(self._add_args(webhook=True))
        assert result["ok"] is True
        assert result["webhook_secret"].startswith("whk_")
        assert result["webhook_url"].endswith("/webhooks/job/new-job")
        stored = job_manage.load_job("new-job")
        assert stored["webhook"]["secret"] == result["webhook_secret"]

    def test_add_invalid_schedule_still_rejected(self):
        result = job_manage.cmd_add(self._add_args(schedule="not a cron"))
        assert result["ok"] is False
