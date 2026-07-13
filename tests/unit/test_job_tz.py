"""Tests for job.tz.job_timezone_default()."""

from zoneinfo import ZoneInfo

from job.tz import job_timezone_default


def test_defaults_to_utc_when_unset(monkeypatch):
    monkeypatch.delenv("JOB_TIMEZONE", raising=False)
    monkeypatch.delenv("CRON_TIMEZONE", raising=False)
    assert job_timezone_default() == ZoneInfo("UTC")


def test_returns_configured_zone(monkeypatch):
    monkeypatch.delenv("CRON_TIMEZONE", raising=False)
    monkeypatch.setenv("JOB_TIMEZONE", "Europe/Paris")
    assert job_timezone_default() == ZoneInfo("Europe/Paris")


def test_falls_back_to_utc_on_invalid(monkeypatch):
    monkeypatch.delenv("CRON_TIMEZONE", raising=False)
    monkeypatch.setenv("JOB_TIMEZONE", "Not/AZone")
    assert job_timezone_default() == ZoneInfo("UTC")


def test_falls_back_to_utc_on_empty(monkeypatch):
    monkeypatch.delenv("CRON_TIMEZONE", raising=False)
    monkeypatch.setenv("JOB_TIMEZONE", "")
    assert job_timezone_default() == ZoneInfo("UTC")


def test_cron_timezone_alias_still_honored(monkeypatch):
    """The deprecated CRON_TIMEZONE env var is still accepted as a fallback."""
    monkeypatch.delenv("JOB_TIMEZONE", raising=False)
    monkeypatch.setenv("CRON_TIMEZONE", "Europe/Paris")
    assert job_timezone_default() == ZoneInfo("Europe/Paris")


def test_job_timezone_takes_precedence_over_cron_alias(monkeypatch):
    """JOB_TIMEZONE wins when both the new var and the deprecated alias are set."""
    monkeypatch.setenv("CRON_TIMEZONE", "America/New_York")
    monkeypatch.setenv("JOB_TIMEZONE", "Europe/Paris")
    assert job_timezone_default() == ZoneInfo("Europe/Paris")
