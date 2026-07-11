"""Template tests for the jobs Performance tab markup on GET /jobs."""

import pytest

pytest.importorskip("croniter")

from fastapi.testclient import TestClient

import main as app_mod


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


def test_jobs_page_contains_performance_tab(client):
    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert 'data-tab="performance"' in resp.text


def test_jobs_page_contains_perf_canvases(client):
    resp = client.get("/jobs")
    html = resp.text
    for canvas_id in (
        "job-perf-timeseries",
        "job-perf-success",
        "job-perf-by-job-duration",
        "job-perf-by-job-cost",
    ):
        assert f'id="{canvas_id}"' in html


def test_jobs_page_loads_shared_chart_module(client):
    """The Performance tab depends on the shared renderers being included."""
    resp = client.get("/jobs")
    assert "/static/perf-charts.js" in resp.text


def test_jobs_page_has_perf_empty_state(client):
    resp = client.get("/jobs")
    assert "No job runs in this range yet" in resp.text


def test_jobs_tab_order_is_jobs_performance_logs(client):
    """Tabs read Jobs, Performance, Logs (Performance before Logs, matching /bot)."""
    html = client.get("/jobs").text
    jobs = html.index('data-tab="jobs"')
    perf = html.index('data-tab="performance"')
    logs = html.index('data-tab="logs"')
    assert jobs < perf < logs


def test_job_logs_rows_are_expandable(client):
    """The Logs tab wires click-to-expand detail rows (parity with the bot logs)."""
    html = client.get("/jobs").text
    assert "toggleLogRow" in html
    assert "job-logdetail-" in html
    assert 'class="row-detail"' in html


def test_job_modal_has_schedule_builder(client):
    """The modal exposes the Repeat dropdown and contextual builder fields."""
    html = client.get("/jobs").text
    assert 'id="field-repeat"' in html
    for field_id in (
        "builder-minutes",
        "builder-hourly",
        "builder-daily",
        "builder-weekly",
        "builder-monthly",
        "builder-custom",
        "field-schedule-raw",
        "weekday-chips",
    ):
        assert f'id="{field_id}"' in html
    # The generated cron still flows through the hidden #field-schedule.
    assert 'id="field-schedule"' in html
    # The builder JS entry points are present.
    assert "builderToCron" in html
    assert "cronToBuilder" in html
    # Per-job timezone selector + browser-default population.
    assert 'id="field-timezone"' in html
    assert "supportedValuesOf" in html
    assert "resolvedOptions().timeZone" in html


def test_weekday_chips_are_clickable(client):
    """Each individual weekday chip toggles via Jobs.toggleWeekday."""
    html = client.get("/jobs").text
    assert html.count('onclick="Jobs.toggleWeekday(this)"') == 7
    assert "toggleWeekday(chip)" in html


def test_modal_dialog_semantics(client):
    """The modal carries dialog ARIA and the close button is labelled."""
    html = client.get("/jobs").text
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-labelledby="modal-title"' in html
    assert 'aria-label="Close"' in html
    # Escape-to-close handler is wired.
    assert "e.key !== 'Escape'" in html


def test_toggle_buttons_announce_state(client):
    """Chips and the segmented action toggle expose aria-pressed."""
    html = client.get("/jobs").text
    # 7 chips start unpressed; segmented has one pressed + one not.
    assert html.count('aria-pressed="false"') >= 8
    assert html.count('aria-pressed="true"') >= 1
    assert 'aria-expanded="false"' in html  # advanced disclosure + action menus


def test_labels_are_associated_with_inputs(client):
    """Form labels use for= so screen readers and label-taps work."""
    html = client.get("/jobs").text
    for field in (
        "field-id",
        "field-repeat",
        "field-timezone",
        "field-prompt",
        "field-command",
        "field-working-dir",
        "field-report-mode",
        "field-max-turns",
        "field-ephemeral",
    ):
        assert f'for="{field}"' in html


def test_grace_minutes_not_in_form(client):
    """grace_minutes is internal/API-only — the modal does not expose it."""
    html = client.get("/jobs").text
    assert "field-grace-minutes" not in html
    assert "Grace Minutes" not in html


def test_schedule_preview_is_live_region(client):
    html = client.get("/jobs").text
    assert 'id="schedule-preview" aria-live="polite"' in html


def test_focus_visible_styles_present(client):
    html = client.get("/jobs").text
    assert "focus-visible" in html


def test_no_off_palette_green(client):
    """Greens trace to the dashboard accent (52,211,153), not the portal green."""
    html = client.get("/jobs").text
    assert "rgba(74, 222, 128" not in html


def test_notify_select_replaces_discord_checkbox(client):
    """One Notify select (always/silent/off); the old checkbox is gone."""
    html = client.get("/jobs").text
    assert 'value="off"' in html
    assert 'value="silent"' in html
    assert "onNotifyChange" in html
    # The misleading enable/disable checkbox is removed.
    assert "field-discord-enabled" not in html
    assert "discord-default-hint" not in html


def test_job_modal_repeat_options(client):
    """The Repeat dropdown offers the six frequency options."""
    html = client.get("/jobs").text
    for value in ("minutes", "hourly", "daily", "weekly", "monthly", "custom"):
        assert f'value="{value}"' in html


def test_job_modal_has_action_toggle_and_command_fields(client):
    """The modal exposes the action toggle and command-job fields."""
    html = client.get("/jobs").text
    assert 'id="field-type"' in html
    assert 'data-type="prompt"' in html
    assert 'data-type="command"' in html
    assert 'id="field-command"' in html
    assert 'id="field-working-dir"' in html
    # Advanced disclosure groups the agent-only options.
    assert 'id="advanced-body"' in html
    # Save & run now button.
    assert "saveAndRun" in html


def test_job_modal_working_dir_placeholder(client):
    """The working-dir field placeholder is the resolved default cwd."""
    import os

    html = client.get("/jobs").text
    expected = os.environ.get("MERLIN_LAUNCH_CWD") or os.path.expanduser("~")
    assert f'placeholder="{expected}"' in html


# ---------------------------------------------------------------------------
# Webhook UI markup + webhook-events endpoint
# ---------------------------------------------------------------------------


def test_job_modal_has_webhook_block(client):
    """The modal exposes the webhook trigger block."""
    html = client.get("/jobs").text
    for field_id in (
        "field-webhook-enabled",
        "webhook-details",
        "field-webhook-url",
        "field-webhook-secret",
        "webhook-reveal-btn",
    ):
        assert field_id in html
    assert "Rotate secret" in html
    assert "Send test" in html


def test_job_modal_has_no_schedule_option(client):
    html = client.get("/jobs").text
    assert 'value="none"' in html
    assert "No schedule (webhook or manual only)" in html


def test_logs_tab_has_trigger_column(client):
    """The Logs tab (execution logs) keeps its Trigger column."""
    html = client.get("/jobs").text
    assert "<th>Trigger</th>" in html


def _write_webhook_job(monkeypatch, tmp_path, webhook=True):
    """Point the page at a temp jobs dir holding one (optionally webhook) job."""
    import json

    from job import manage as job_manage

    monkeypatch.setattr(job_manage, "JOBS_DIR", tmp_path)
    data = {"type": "command", "command": "echo hi", "enabled": True}
    if webhook:
        data["webhook"] = {"secret": "whk_x"}
    (tmp_path / "wh.json").write_text(json.dumps(data))


def test_webhooks_tab_hidden_without_webhook(client, tmp_path, monkeypatch):
    """No webhook-firable job → no Webhooks tab (no clutter)."""
    _write_webhook_job(monkeypatch, tmp_path, webhook=False)
    html = client.get("/jobs").text
    assert 'data-tab="webhooks"' not in html
    assert 'id="job-tab-webhooks"' not in html


def test_webhooks_tab_shown_with_webhook(client, tmp_path, monkeypatch):
    """A webhook-firable job surfaces the dedicated Webhooks tab + activity."""
    _write_webhook_job(monkeypatch, tmp_path, webhook=True)
    html = client.get("/jobs").text
    assert 'data-tab="webhooks"' in html
    assert 'id="job-tab-webhooks"' in html
    assert "job-webhook-activity" in html
    assert "loadWebhookActivity" in html


def test_webhook_events_endpoint_filters_job_source(client, tmp_path, monkeypatch):
    """/api/job/webhook-events returns job-source events newest first,
    including rejected attempts, filterable by job id."""
    import json as json_mod

    from lib import event_log

    lines = [
        {
            "type": "webhook_request",
            "timestamp": "2026-07-10T10:00:00+00:00",
            "source": "job",
            "target": "triage",
            "ip": "1.2.3.4",
            "outcome": "launched",
            "run_id": "r1",
        },
        {
            "type": "webhook_request",
            "timestamp": "2026-07-10T11:00:00+00:00",
            "source": "job",
            "target": "triage",
            "ip": "6.6.6.6",
            "outcome": "rejected_secret",
        },
        {
            "type": "webhook_request",
            "timestamp": "2026-07-10T12:00:00+00:00",
            "source": "other",
            "target": "triage",
            "ip": "1.2.3.4",
            "outcome": "launched",
        },
        {
            "type": "webhook_request",
            "timestamp": "2026-07-10T13:00:00+00:00",
            "source": "job",
            "target": "another",
            "ip": "1.2.3.4",
            "outcome": "coalesced",
        },
    ]
    path = tmp_path / "engine-log.jsonl"
    path.write_text("".join(json_mod.dumps(e) + "\n" for e in lines))
    monkeypatch.setattr(event_log, "ENGINE_LOG_PATH", path)

    resp = client.get("/api/job/webhook-events")
    assert resp.status_code == 200
    events = resp.json()
    # Only source=job, newest first
    assert [e["outcome"] for e in events] == [
        "coalesced",
        "rejected_secret",
        "launched",
    ]

    resp = client.get("/api/job/webhook-events?job_id=triage")
    events = resp.json()
    assert len(events) == 2
    assert all(e["target"] == "triage" for e in events)

    resp = client.get("/api/job/webhook-events?job_id=triage&limit=1")
    assert len(resp.json()) == 1


def test_job_modal_has_public_url_hint(client):
    """The reachability hint (shown only for ip-derived URLs) is present."""
    html = client.get("/jobs").text
    assert 'id="webhook-url-hint"' in html
    assert "Set a public URL in Settings" in html
