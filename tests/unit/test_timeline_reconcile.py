"""Independent activity-capture consent and provider hook reconciliation."""

from __future__ import annotations

import json
import fcntl
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import paths
from lib import hook_files as shared_hook_files
from timeline import consent, reconcile


@pytest.fixture
def hook_files(tmp_path, monkeypatch):
    claude = tmp_path / "claude" / "settings.json"
    codex = tmp_path / "codex" / "hooks.json"
    monkeypatch.setattr(reconcile, "claude_settings_path", lambda: claude)
    monkeypatch.setattr(reconcile, "codex_hooks_path", lambda: codex)
    return claude, codex


def read(path):
    return json.loads(path.read_text())


def owned_groups(value: dict) -> list[dict]:
    return [
        group
        for groups in value.get("hooks", {}).values()
        for group in groups
        if reconcile._is_owned(group)
    ]


def test_consent_defaults_to_ask_and_is_separate_from_state_pills(monkeypatch):
    monkeypatch.delenv(consent.CAPTURE_KEY, raising=False)
    monkeypatch.setenv("AGENT_STATE_HOOKS", "auto")
    assert consent.capture_mode() == "ask"
    consent.set_capture_mode("off")
    text = paths.config_path().read_text()
    assert "MERLIN_ACTIVITY_HOOKS=off" in text
    assert "AGENT_STATE_HOOKS" not in text
    assert paths.config_path().stat().st_mode & 0o777 == 0o600


def test_consent_preserves_other_config_and_rejects_unknown_mode():
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text("OTHER=value\nMERLIN_ACTIVITY_HOOKS=ask\n")
    assert consent.set_capture_mode("AUTO") == "auto"
    assert (
        paths.config_path().read_text() == "OTHER=value\nMERLIN_ACTIVITY_HOOKS=auto\n"
    )
    with pytest.raises(ValueError):
        consent.set_capture_mode("yes")


def test_saved_consent_overrides_stale_inherited_environment(monkeypatch):
    monkeypatch.setenv(consent.CAPTURE_KEY, "auto")
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text("MERLIN_ACTIVITY_HOOKS=off\n")

    assert consent.capture_setting() == ("off", "config")


def test_install_creates_separate_marked_groups_for_both_providers(hook_files):
    claude, codex = hook_files
    assert reconcile.install_hooks() is True
    assert set(read(claude)["hooks"]) == set(reconcile.CLAUDE_EVENTS)
    assert set(read(codex)["hooks"]) == set(reconcile.CODEX_EVENTS)
    assert len(owned_groups(read(claude))) == len(reconcile.CLAUDE_EVENTS)
    assert len(owned_groups(read(codex))) == len(reconcile.CODEX_EVENTS)
    assert all(
        "activity_hook.py" in group["hooks"][0]["command"]
        and " claude " in group["hooks"][0]["command"]
        for group in owned_groups(read(claude))
    )
    assert all(
        "activity_hook.py" in group["hooks"][0]["command"]
        and " codex " in group["hooks"][0]["command"]
        for group in owned_groups(read(codex))
    )
    assert read(codex)["hooks"]["SessionStart"][-1]["matcher"] == "startup|resume|clear"


def test_foreign_and_state_pill_groups_are_preserved_exactly(hook_files):
    claude, _codex = hook_files
    claude.parent.mkdir(parents=True)
    pill = {
        "matcher": "AskUserQuestion|ExitPlanMode",
        "hooks": [{"type": "command", "command": "pill # merlin:agent-state-pill:v4"}],
    }
    foreign = {"hooks": [{"type": "command", "command": "foreign --keep"}]}
    claude.write_text(
        json.dumps(
            {"model": "opus", "hooks": {"Stop": [foreign], "PreToolUse": [pill]}}
        )
    )
    reconcile.install_hooks()
    value = read(claude)
    assert value["model"] == "opus"
    assert value["hooks"]["Stop"][0] == foreign
    assert value["hooks"]["PreToolUse"][0] == pill
    reconcile.remove_hooks()
    removed = read(claude)
    assert removed["hooks"]["Stop"] == [foreign]
    assert removed["hooks"]["PreToolUse"] == [pill]


def test_install_update_drift_remove_and_permissions(hook_files, monkeypatch):
    claude, codex = hook_files
    assert reconcile.install_hooks() is True
    before = claude.read_text()
    assert reconcile.install_hooks() is False
    assert claude.read_text() == before
    assert reconcile.hooks_drift() is False
    monkeypatch.setattr(reconcile, "HOOK_VERSION", 3)
    assert reconcile.hooks_drift() is True
    assert reconcile.install_hooks() is True
    assert ":v3" in claude.read_text()
    assert reconcile.remove_hooks() is True
    assert read(claude) == {}
    assert read(codex) == {}
    assert claude.stat().st_mode & 0o777 == 0o600


def test_install_retires_owned_tool_hook_groups(hook_files):
    claude, _codex = hook_files
    claude.parent.mkdir(parents=True)
    legacy = {
        "matcher": "Read|Write|Bash",
        "hooks": [
            {
                "type": "command",
                "command": "old activity hook # merlin:activity-timeline:v1",
            }
        ],
    }
    claude.write_text(json.dumps({"hooks": {"PostToolUse": [legacy]}}))

    assert reconcile.install_hooks() is True

    value = read(claude)
    assert "PostToolUse" not in value["hooks"]
    assert set(value["hooks"]) == set(reconcile.CLAUDE_EVENTS)


def test_malformed_provider_file_is_untouched_and_other_provider_installs(hook_files):
    claude, codex = hook_files
    claude.parent.mkdir(parents=True)
    claude.write_text("{broken")
    assert reconcile.install_hooks() is True
    assert claude.read_text() == "{broken"
    assert owned_groups(read(codex))


@pytest.mark.parametrize("provider_index", [0, 1])
def test_null_provider_event_is_treated_as_an_empty_group_list(
    hook_files, provider_index
):
    path = hook_files[provider_index]
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"Stop": None}}))

    assert reconcile.install_hooks() is True

    stop = read(path)["hooks"]["Stop"]
    assert isinstance(stop, list)
    assert any(reconcile._is_owned(group) for group in stop)


def test_independent_provider_write_failure_does_not_block_other(
    hook_files, monkeypatch
):
    claude, codex = hook_files
    original = reconcile._write

    def fail_claude(path, value, **kwargs):
        if path == claude:
            raise OSError("read only")
        return original(path, value, **kwargs)

    monkeypatch.setattr(reconcile, "_write", fail_claude)
    assert reconcile.install_hooks() is True
    assert not claude.exists()
    assert owned_groups(read(codex))


def test_external_change_between_read_and_replace_is_never_overwritten(
    hook_files, monkeypatch
):
    claude, _codex = hook_files
    claude.parent.mkdir(parents=True)
    claude.write_text('{"model":"before"}\n')
    original = reconcile._installed

    def concurrent_change(settings, provider, events):
        desired = original(settings, provider, events)
        claude.write_text('{"model":"user-change"}\n')
        return desired

    monkeypatch.setattr(reconcile, "_installed", concurrent_change)

    assert (
        reconcile._reconcile_file(claude, "claude", reconcile.CLAUDE_EVENTS, True)
        is False
    )
    assert read(claude) == {"model": "user-change"}
    assert (
        claude.with_name("settings.json.merlin-lock").stat().st_mode & 0o777
    ) == 0o600


def test_sync_modes_never_install_from_old_state_consent(hook_files, monkeypatch):
    claude, _codex = hook_files
    monkeypatch.setenv("AGENT_STATE_HOOKS", "auto")
    monkeypatch.delenv(consent.CAPTURE_KEY, raising=False)
    assert reconcile.sync_hooks() == "pending"
    assert not claude.exists()
    consent.set_capture_mode("auto")
    assert reconcile.sync_hooks() == "synced"
    consent.set_capture_mode("off")
    assert reconcile.sync_hooks() == "removed"
    assert read(claude) == {}


def test_sync_fails_open_when_consent_cannot_be_read(monkeypatch):
    monkeypatch.setattr(
        reconcile,
        "capture_mode",
        lambda: (_ for _ in ()).throw(OSError("config unavailable")),
    )

    assert reconcile.sync_hooks() == "error"


def test_provider_paths_honor_isolated_environment_roots(tmp_path, monkeypatch):
    from lib import skills

    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert reconcile.claude_settings_path() == claude_home / "settings.json"
    assert skills.claude_settings_path() == claude_home / "settings.json"
    assert reconcile.codex_hooks_path() == codex_home / "hooks.json"
    assert skills.codex_hooks_path() == codex_home / "hooks.json"


def test_core_and_timeline_writers_share_one_provider_lock(tmp_path, monkeypatch):
    from lib import skills

    claude = tmp_path / "claude" / "settings.json"
    codex = tmp_path / "codex" / "hooks.json"
    monkeypatch.setattr(reconcile, "claude_settings_path", lambda: claude)
    monkeypatch.setattr(reconcile, "codex_hooks_path", lambda: codex)
    monkeypatch.setattr(skills, "claude_settings_path", lambda: claude)
    monkeypatch.setattr(skills, "codex_hooks_path", lambda: codex)
    entered = threading.Event()
    release = threading.Event()
    original = skills._reconcile_install

    def pause_while_core_holds_lock(settings, events):
        entered.set()
        assert release.wait(timeout=2)
        return original(settings, events)

    monkeypatch.setattr(skills, "_reconcile_install", pause_while_core_holds_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        core = pool.submit(skills.install_interactive_hooks)
        assert entered.wait(timeout=2)
        activity = pool.submit(reconcile.install_hooks)
        release.set()
        assert core.result(timeout=2) is True
        assert activity.result(timeout=2) is True

    commands = [
        entry["command"]
        for group in read(claude)["hooks"]["UserPromptSubmit"]
        for entry in group["hooks"]
    ]
    assert any(skills._HOOK_MARKER in command for command in commands)
    assert any(reconcile.HOOK_MARKER in command for command in commands)


def test_provider_hook_lock_contention_uses_a_bounded_budget(tmp_path, monkeypatch):
    path = tmp_path / "claude" / "settings.json"
    path.parent.mkdir(parents=True)
    lock_path = path.with_name(path.name + ".merlin-lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    monkeypatch.setattr(shared_hook_files, "LOCK_TIMEOUT_SECONDS", 0.02)
    started = time.monotonic()
    try:
        with pytest.raises(shared_hook_files.ProviderHookLockTimeout):
            with shared_hook_files.provider_hook_lock(path):
                pytest.fail("a contended provider lock must not be acquired")
    finally:
        os.close(lock_fd)

    assert time.monotonic() - started < 0.5
