"""Tests for job.tz.cron_timezone()."""

from zoneinfo import ZoneInfo

from job.tz import cron_timezone


def test_defaults_to_utc_when_unset(monkeypatch):
    monkeypatch.delenv("CRON_TIMEZONE", raising=False)
    assert cron_timezone() == ZoneInfo("UTC")


def test_returns_configured_zone(monkeypatch):
    monkeypatch.setenv("CRON_TIMEZONE", "Europe/Paris")
    assert cron_timezone() == ZoneInfo("Europe/Paris")


def test_falls_back_to_utc_on_invalid(monkeypatch):
    monkeypatch.setenv("CRON_TIMEZONE", "Not/AZone")
    assert cron_timezone() == ZoneInfo("UTC")


def test_falls_back_to_utc_on_empty(monkeypatch):
    monkeypatch.setenv("CRON_TIMEZONE", "")
    assert cron_timezone() == ZoneInfo("UTC")
