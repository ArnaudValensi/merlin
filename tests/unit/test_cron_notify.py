"""Tests for cron.notify — notification with graceful fallback to Discord."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from cron.notify import (
    _format_report,
    _get_bot_default_channel,
    notify_cron_result,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeExtensionInfo:
    """Minimal stand-in for main.ExtensionInfo."""

    id: str = "merlin-bot"
    tier: str = "built-in"
    enabled: bool = True
    loaded: bool = True
    error: str | None = None
    meta: dict = field(default_factory=dict)
    has_start: bool = False
    has_tunnel_hook: bool = False
    module: object | None = None


def _make_bot_module(*, channels: set[str] | None = None, notify_side_effect=None):
    """Create a fake bot module with DISCORD_CHANNEL_IDS and notify()."""
    mod = MagicMock()
    mod.DISCORD_CHANNEL_IDS = channels if channels is not None else {"111222333"}
    if notify_side_effect:
        mod.notify.side_effect = notify_side_effect
    return mod


def _make_registry(bot_info=None) -> dict:
    """Build an extension_registry dict."""
    if bot_info is None:
        return {}
    return {"merlin-bot": bot_info}


def _sample_job(**overrides) -> dict:
    """Sample job in NEW format (no 'channel' field — uses discord_channel)."""
    defaults = {
        "description": "Daily check",
        "schedule": "0 9 * * *",
        "prompt": "Do the thing",
    }
    defaults.update(overrides)
    return defaults


def _sample_channel_job(**overrides) -> dict:
    """Sample job with legacy 'channel' field."""
    defaults = {
        "description": "Daily check",
        "schedule": "0 9 * * *",
        "prompt": "Do the thing",
        "channel": "999",
    }
    defaults.update(overrides)
    return defaults


def _sample_result(**overrides) -> dict:
    defaults = {
        "exit_code": 0,
        "duration_seconds": 12.3,
        "cost_usd": 0.0042,
        "output": "All good.",
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Tests: notify_cron_result
# ---------------------------------------------------------------------------


class TestNotifyCronResult:
    """Tests for the top-level notify_cron_result function."""

    def test_bot_loaded_channel_configured(self):
        """Bot loaded + channel configured -> notify() called with correct args."""
        mod = _make_bot_module(channels={"444555666"})
        info = FakeExtensionInfo(loaded=True, module=mod)
        registry = _make_registry(info)

        notify_cron_result("daily-check", _sample_job(), _sample_result(), registry)

        mod.notify.assert_called_once()
        call_args = mod.notify.call_args
        assert call_args[0][0] == "444555666"  # channel
        assert "daily-check" in call_args[0][1]  # message contains job_id

    def test_session_id_passed_to_notify(self):
        """Session ID from result is forwarded to bot.notify() for continuity."""
        mod = _make_bot_module(channels={"444555666"})
        info = FakeExtensionInfo(loaded=True, module=mod)
        registry = _make_registry(info)

        result = _sample_result(session_id="abc-123")
        notify_cron_result("daily-check", _sample_job(), result, registry)

        mod.notify.assert_called_once()
        assert mod.notify.call_args.kwargs["session_id"] == "abc-123"

    def test_no_session_id_passes_none(self):
        """When result has no session_id, None is passed."""
        mod = _make_bot_module(channels={"444555666"})
        info = FakeExtensionInfo(loaded=True, module=mod)
        registry = _make_registry(info)

        notify_cron_result("daily-check", _sample_job(), _sample_result(), registry)

        mod.notify.assert_called_once()
        assert mod.notify.call_args.kwargs["session_id"] is None

    def test_bot_loaded_no_channel(self):
        """Bot loaded but no channel configured -> no notification, no error."""
        mod = _make_bot_module(channels=set())
        info = FakeExtensionInfo(loaded=True, module=mod)
        registry = _make_registry(info)

        # Should not raise
        notify_cron_result("daily-check", _sample_job(), _sample_result(), registry)
        mod.notify.assert_not_called()

    def test_bot_not_in_registry(self):
        """Bot not in registry -> no notification, no error."""
        registry: dict = {}
        # Should not raise
        notify_cron_result("daily-check", _sample_job(), _sample_result(), registry)

    def test_bot_in_registry_not_loaded(self):
        """Bot in registry but loaded=False -> no notification."""
        mod = _make_bot_module()
        info = FakeExtensionInfo(loaded=False, module=mod)
        registry = _make_registry(info)

        notify_cron_result("daily-check", _sample_job(), _sample_result(), registry)
        mod.notify.assert_not_called()

    def test_bot_in_registry_module_none(self):
        """Bot in registry but module=None -> no notification."""
        info = FakeExtensionInfo(loaded=True, module=None)
        registry = _make_registry(info)

        notify_cron_result("daily-check", _sample_job(), _sample_result(), registry)

    def test_notify_raises_exception_logged_no_crash(self, caplog):
        """Bot notify() raises -> exception logged, no crash."""
        mod = _make_bot_module(notify_side_effect=RuntimeError("Discord down"))
        info = FakeExtensionInfo(loaded=True, module=mod)
        registry = _make_registry(info)

        with caplog.at_level(logging.DEBUG, logger="merlin.cron"):
            notify_cron_result("daily-check", _sample_job(), _sample_result(), registry)

        assert any("Discord notification failed" in r.message for r in caplog.records)

    def test_per_job_discord_channel_overrides_global(self):
        """Per-job discord_channel overrides bot's global default."""
        mod = _make_bot_module(channels={"111222333"})
        info = FakeExtensionInfo(loaded=True, module=mod)
        registry = _make_registry(info)

        job = _sample_job(discord_channel="777888999")
        notify_cron_result("daily-check", job, _sample_result(), registry)

        mod.notify.assert_called_once()
        assert mod.notify.call_args[0][0] == "777888999"

    def test_global_channel_used_when_per_job_not_set(self):
        """Global channel used when per-job not set."""
        mod = _make_bot_module(channels={"111222333"})
        info = FakeExtensionInfo(loaded=True, module=mod)
        registry = _make_registry(info)

        job = _sample_job()  # No discord_channel key
        assert "discord_channel" not in job

        notify_cron_result("daily-check", job, _sample_result(), registry)

        mod.notify.assert_called_once()
        assert mod.notify.call_args[0][0] == "111222333"

    def test_legacy_channel_field_used(self):
        """Jobs with legacy 'channel' field use it for notification."""
        mod = _make_bot_module(channels={"111222333"})
        info = FakeExtensionInfo(loaded=True, module=mod)
        registry = _make_registry(info)

        notify_cron_result("daily-check", _sample_channel_job(), _sample_result(), registry)
        mod.notify.assert_called_once()
        assert mod.notify.call_args[0][0] == "999"

    def test_silent_mode_skips_on_success(self):
        """report_mode=silent skips notification on successful jobs."""
        mod = _make_bot_module(channels={"111222333"})
        info = FakeExtensionInfo(loaded=True, module=mod)
        registry = _make_registry(info)

        job = _sample_job(report_mode="silent")
        notify_cron_result("daily-check", job, _sample_result(exit_code=0), registry)
        mod.notify.assert_not_called()

    def test_silent_mode_notifies_on_error(self):
        """report_mode=silent still notifies on failed jobs."""
        mod = _make_bot_module(channels={"111222333"})
        info = FakeExtensionInfo(loaded=True, module=mod)
        registry = _make_registry(info)

        job = _sample_job(report_mode="silent")
        notify_cron_result("daily-check", job, _sample_result(exit_code=1), registry)
        mod.notify.assert_called_once()

    def test_always_mode_notifies_on_success(self):
        """report_mode=always notifies even on success (default behavior)."""
        mod = _make_bot_module(channels={"111222333"})
        info = FakeExtensionInfo(loaded=True, module=mod)
        registry = _make_registry(info)

        job = _sample_job(report_mode="always")
        notify_cron_result("daily-check", job, _sample_result(exit_code=0), registry)
        mod.notify.assert_called_once()

    def test_outer_exception_caught_and_logged(self, caplog):
        """Even if _do_notify raises unexpectedly, notify_cron_result never raises."""
        registry = {"merlin-bot": "not-an-extension-info"}  # Will cause AttributeError

        with caplog.at_level(logging.DEBUG, logger="merlin.cron"):
            notify_cron_result("daily-check", _sample_job(), _sample_result(), registry)

        assert any("Notification failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Tests: _get_bot_default_channel
# ---------------------------------------------------------------------------


class TestGetBotDefaultChannel:
    """Tests for _get_bot_default_channel."""

    def test_returns_first_channel(self):
        mod = MagicMock()
        mod.DISCORD_CHANNEL_IDS = {"abc123"}
        assert _get_bot_default_channel(mod) == "abc123"

    def test_empty_channels_returns_none(self):
        mod = MagicMock()
        mod.DISCORD_CHANNEL_IDS = set()
        assert _get_bot_default_channel(mod) is None

    def test_no_attribute_returns_none(self):
        """Module without DISCORD_CHANNEL_IDS -> None."""
        mod = object()  # Plain object, no attributes
        assert _get_bot_default_channel(mod) is None


# ---------------------------------------------------------------------------
# Tests: _format_report
# ---------------------------------------------------------------------------


class TestFormatReport:
    """Tests for _format_report."""

    def test_success_report(self):
        msg = _format_report("daily-check", _sample_job(), _sample_result())
        assert "\u2705" in msg  # checkmark
        assert "Daily check" in msg
        assert "daily-check" in msg
        assert "12.3s" in msg
        assert "$0.0042" in msg
        assert "All good." in msg

    def test_failure_report(self):
        result = _sample_result(exit_code=1)
        msg = _format_report("daily-check", _sample_job(), result)
        assert "\u274c" in msg  # red X

    def test_no_cost(self):
        result = _sample_result(cost_usd=None)
        msg = _format_report("daily-check", _sample_job(), result)
        assert "Cost" not in msg

    def test_zero_cost_omitted(self):
        """cost_usd=0 is falsy so should not show cost."""
        result = _sample_result(cost_usd=0)
        msg = _format_report("daily-check", _sample_job(), result)
        assert "Cost" not in msg

    def test_long_output_not_truncated(self):
        """Full output is included — send_message() handles chunking."""
        long_output = "x" * 3000
        result = _sample_result(output=long_output)
        msg = _format_report("daily-check", _sample_job(), result)
        assert "x" * 3000 in msg

    def test_empty_output(self):
        result = _sample_result(output="")
        msg = _format_report("daily-check", _sample_job(), result)
        assert "```" not in msg  # No code block when no output

    def test_no_output_key(self):
        result = {"exit_code": 0, "duration_seconds": 1.0}
        msg = _format_report("daily-check", _sample_job(), result)
        assert "```" not in msg

    def test_description_fallback_to_job_id(self):
        """When job has no description, job_id is used."""
        job = _sample_job()
        del job["description"]
        msg = _format_report("my-job", job, _sample_result())
        # The description fallback means "my-job" appears as both desc and id
        assert "my-job" in msg
