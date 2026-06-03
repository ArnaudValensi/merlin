"""Template tests for the cron Performance tab markup on GET /cron."""

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


def test_cron_page_contains_performance_tab(client):
    resp = client.get("/cron")
    assert resp.status_code == 200
    assert 'data-tab="performance"' in resp.text


def test_cron_page_contains_perf_canvases(client):
    resp = client.get("/cron")
    html = resp.text
    for canvas_id in (
        "cron-perf-timeseries",
        "cron-perf-success",
        "cron-perf-by-job-duration",
        "cron-perf-by-job-cost",
    ):
        assert f'id="{canvas_id}"' in html


def test_cron_page_loads_shared_chart_module(client):
    """The Performance tab depends on the shared renderers being included."""
    resp = client.get("/cron")
    assert "/static/perf-charts.js" in resp.text


def test_cron_page_has_perf_empty_state(client):
    resp = client.get("/cron")
    assert "No cron runs in this range yet" in resp.text


def test_cron_tab_order_is_jobs_performance_logs(client):
    """Tabs read Jobs, Performance, Logs (Performance before Logs, matching /bot)."""
    html = client.get("/cron").text
    jobs = html.index('data-tab="jobs"')
    perf = html.index('data-tab="performance"')
    logs = html.index('data-tab="logs"')
    assert jobs < perf < logs


def test_cron_logs_rows_are_expandable(client):
    """The Logs tab wires click-to-expand detail rows (parity with the bot logs)."""
    html = client.get("/cron").text
    assert "toggleLogRow" in html
    assert "cron-logdetail-" in html
    assert 'class="row-detail"' in html


def test_cron_modal_has_schedule_builder(client):
    """The modal exposes the Repeat dropdown and contextual builder fields."""
    html = client.get("/cron").text
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
    """Each individual weekday chip toggles via Cron.toggleWeekday."""
    html = client.get("/cron").text
    assert html.count('onclick="Cron.toggleWeekday(this)"') == 7
    assert "toggleWeekday(chip)" in html


def test_cron_modal_repeat_options(client):
    """The Repeat dropdown offers the six frequency options."""
    html = client.get("/cron").text
    for value in ("minutes", "hourly", "daily", "weekly", "monthly", "custom"):
        assert f'value="{value}"' in html


def test_cron_modal_has_action_toggle_and_command_fields(client):
    """The modal exposes the action toggle and command-job fields."""
    html = client.get("/cron").text
    assert 'id="field-type"' in html
    assert 'data-type="prompt"' in html
    assert 'data-type="command"' in html
    assert 'id="field-command"' in html
    assert 'id="field-working-dir"' in html
    # Advanced disclosure groups the agent-only options.
    assert 'id="advanced-body"' in html
    # Save & run now button.
    assert "saveAndRun" in html


def test_cron_modal_working_dir_placeholder(client):
    """The working-dir field placeholder is the resolved default cwd."""
    import os

    html = client.get("/cron").text
    expected = os.environ.get("MERLIN_LAUNCH_CWD") or os.path.expanduser("~")
    assert f'placeholder="{expected}"' in html
