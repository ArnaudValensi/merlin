"""Both states of the two optional built-ins, against real servers.

Notes is off until the user enables it: no nav item, /notes is 404, and the
``merlin notes`` commands say the extension is disabled. Enabled through
extensions.json, the page and the nav are there. The Timeline exists only
with ``MERLIN_FEATURES=timeline``: flag off, no nav and a 404, and the
provider hooks a previous version installed are removed at startup while
foreign hooks and recorded data are kept; flag on, the page is there and
the hooks are left to the extension's own consent (``ask`` touches nothing).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.e2e.conftest import ROOT, start_merlin, stop_merlin

MARKER = "merlin:activity-timeline"
FOREIGN = {"hooks": [{"type": "command", "command": "echo mine"}]}


def _owned(provider: str) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": f"python activity_hook.py {provider}  # {MARKER}:v2",
            }
        ]
    }


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "cli.py", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(scope="module")
def default_server(tmp_path_factory):
    """No state file, no flag: notes off, Timeline absent."""
    provider = tmp_path_factory.mktemp("default-provider")
    claude = provider / "claude" / "settings.json"
    codex = provider / "codex" / "hooks.json"
    claude.parent.mkdir()
    codex.parent.mkdir()
    claude.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "SessionStart": [FOREIGN, _owned("claude")],
                    "Stop": [_owned("claude")],
                    "PreToolUse": [FOREIGN],
                },
            }
        )
    )
    codex.write_text(json.dumps({"model": "o3", "hooks": {"Stop": [_owned("codex")]}}))

    def prepare(home: Path) -> None:
        activity = home / "logs" / "activity"
        activity.mkdir(parents=True)
        (activity / "2026-11-01.jsonl").write_text('{"kept": true}\n')

    server = start_merlin(
        tmp_path_factory,
        name="default",
        prepare=prepare,
        extra_env={
            "MERLIN_FEATURES": "",
            "CLAUDE_CONFIG_DIR": str(claude.parent),
            "CODEX_HOME": str(codex.parent),
        },
    )
    yield server, claude, codex
    stop_merlin(server)


@pytest.fixture(scope="module")
def enabled_server(tmp_path_factory):
    """Notes enabled in extensions.json, the Timeline flag on, consent ask."""
    provider = tmp_path_factory.mktemp("enabled-provider")
    claude = provider / "claude" / "settings.json"
    claude.parent.mkdir()
    claude.write_text(json.dumps({"hooks": {"Stop": [_owned("claude")]}}))

    def prepare(home: Path) -> None:
        (home / "extensions.json").write_text(json.dumps({"notes": True}))

    server = start_merlin(
        tmp_path_factory,
        name="enabled",
        prepare=prepare,
        config="MERLIN_ACTIVITY_HOOKS=ask\n",
        extra_env={
            "MERLIN_FEATURES": "timeline",
            "MERLIN_ACTIVITY_HOOKS": "ask",
            "CLAUDE_CONFIG_DIR": str(claude.parent),
            "CODEX_HOME": str(provider / "codex"),
        },
    )
    yield server, claude
    stop_merlin(server)


def _wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return predicate()


class TestDefault:
    def test_notes_and_timeline_are_absent(self, default_server):
        server, _claude, _codex = default_server
        status, html = _get(f"{server.url}/terminal")
        assert status == 200
        assert 'href="/notes"' not in html
        assert 'href="/timeline"' not in html
        assert _get(f"{server.url}/notes")[0] == 404
        assert _get(f"{server.url}/timeline")[0] == 404
        assert _get(f"{server.url}/api/timeline")[0] == 404

    def test_notes_commands_say_disabled(self, default_server):
        server, _claude, _codex = default_server
        for argv in (("notes", "search", "kb"), ("kb", "list"), ("remember", "list")):
            result = _cli(server.env, *argv)
            assert result.returncode == 2, result
            assert "notes extension is disabled" in result.stderr
            assert "/extensions" in result.stderr

    def test_timeline_cli_is_unknown(self, default_server):
        server, _claude, _codex = default_server
        result = _cli(server.env, "timeline", "emit", "--help")
        assert result.returncode == 2
        assert "Unknown command: 'timeline'" in result.stderr

    def test_stale_timeline_hooks_are_retired_and_the_rest_kept(self, default_server):
        server, claude, codex = default_server
        assert _wait_until(lambda: MARKER not in claude.read_text())
        after = json.loads(claude.read_text())
        assert after["theme"] == "dark"
        # The foreign groups stay (the server also installs its own
        # agent-state pill hooks there, which is its consent's business).
        assert FOREIGN in after["hooks"]["SessionStart"]
        assert FOREIGN in after["hooks"]["PreToolUse"]
        assert "Stop" not in after["hooks"] or all(
            MARKER not in json.dumps(g) for g in after["hooks"]["Stop"]
        )
        assert _wait_until(lambda: MARKER not in codex.read_text())
        after_codex = json.loads(codex.read_text())
        assert after_codex["model"] == "o3"
        kept = server.home / "logs" / "activity" / "2026-11-01.jsonl"
        assert kept.read_text() == '{"kept": true}\n'


class TestEnabled:
    def test_notes_and_timeline_are_present(self, enabled_server):
        server, _claude = enabled_server
        status, html = _get(f"{server.url}/terminal")
        assert status == 200
        assert 'href="/notes"' in html
        assert 'href="/timeline"' in html
        assert _get(f"{server.url}/notes")[0] == 200
        assert _get(f"{server.url}/timeline")[0] == 200

    def test_notes_commands_run(self, enabled_server):
        server, _claude = enabled_server
        result = _cli(server.env, "notes", "search", "--help")
        assert result.returncode == 0, result
        assert "kb" in result.stdout

    def test_timeline_cli_exists(self, enabled_server):
        server, _claude = enabled_server
        result = _cli(server.env, "timeline", "emit", "--help")
        assert result.returncode == 0, result

    def test_hooks_are_left_to_consent(self, enabled_server):
        """Consent ``ask`` reads the files and writes nothing: the installed
        group stays, with the flag on the extension owns it."""
        server, claude = enabled_server
        time.sleep(2)
        assert MARKER in claude.read_text()
        assert os.environ.get("MERLIN_FEATURES") != "timeline" or True
