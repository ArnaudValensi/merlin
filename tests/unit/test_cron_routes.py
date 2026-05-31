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
