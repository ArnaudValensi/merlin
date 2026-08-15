"""Sanitization and correlation contracts for provider hook fixtures."""

import json
from pathlib import Path

import pytest


FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "activity_hooks"
CONTENT_FIELDS = {
    "error",
    "last_assistant_message",
    "message",
    "prompt",
    "title",
    "tool_input",
    "tool_response",
}
FORBIDDEN_FRAGMENTS = (
    "sk-",
    "ghp_",
    "BEGIN PRIVATE KEY",
    "password=",
    "authorization:",
    "/home/arnaud/",
)


def _load(provider: str) -> list[dict]:
    return json.loads((FIXTURE_DIR / f"{provider}.json").read_text())


def _is_redacted(value: object) -> bool:
    return value == "<redacted>" or (
        isinstance(value, dict) and value.get("_redacted") is True
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_fixture_content_fields_are_redacted(provider):
    cases = _load(provider)
    assert cases
    serialized = json.dumps(cases).lower()
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment.lower() not in serialized

    for case in cases:
        payload = case["payload"]
        for field in CONTENT_FIELDS & payload.keys():
            assert _is_redacted(payload[field]), (
                f"{provider}:{case['case']} leaks {field}"
            )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_fixture_ids_support_safe_provider_scoped_correlation(provider):
    cases = _load(provider)
    payloads = {case["case"]: case["payload"] for case in cases}

    session_ids = {payload["session_id"] for payload in payloads.values()}
    assert session_ids == {f"{provider}-session-a"}

    if provider == "claude":
        turn_key = "prompt_id"
    else:
        turn_key = "turn_id"
    turn_ids = {
        payload[turn_key] for payload in payloads.values() if turn_key in payload
    }
    assert turn_ids == {f"{provider}-{'prompt' if provider == 'claude' else 'turn'}-1"}

    assert (
        payloads["tool_start"]["tool_use_id"]
        == payloads["tool_failure" if provider == "claude" else "tool_finish"][
            "tool_use_id"
        ]
    )
    assert "tool_use_id" not in payloads["permission_requested"]
    assert (
        payloads["session_resume"]["session_id"]
        == payloads["session_start_startup"]["session_id"]
    )
    assert payloads["session_compact"]["source"] == "compact"
