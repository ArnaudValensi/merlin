"""Tests for the core `sessions` viewer module.

The transcript viewer used to live in merlin-bot; it is now a core module at
/session + /api/session so jobs and the bot both reach it without depending on
the bot being enabled.
"""

import json

import pytest
from fastapi.testclient import TestClient

import auth
import main as app_mod
from sessions import routes as sessions_routes


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")


@pytest.fixture(autouse=True)
def _redirect_raw_sessions(tmp_path, monkeypatch):
    session_dir = tmp_path / "raw-sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(sessions_routes, "RAW_SESSION_DIR", session_dir)
    return session_dir


@pytest.fixture
def client():
    return TestClient(app_mod.app)


@pytest.fixture
def sample_session(_redirect_raw_sessions):
    filename = "2026-02-06_12-00-00-test-sess-abc.jsonl"
    content = (
        "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init", "model": "opus"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Hello"}]},
                    }
                ),
                json.dumps({"type": "result", "subtype": "success", "num_turns": 1}),
            ]
        )
        + "\n"
    )
    (_redirect_raw_sessions / filename).write_text(content)
    return filename


# ---------------------------------------------------------------------------
# Routing — the module resolves at /session + /api/session
# ---------------------------------------------------------------------------


def test_routes_resolve_under_session_slug():
    paths = {r.path for r in app_mod.app.routes if hasattr(r, "path")}
    assert "/session/{filename}" in paths
    assert "/api/session/{filename}" in paths


# ---------------------------------------------------------------------------
# GET /api/session/{filename}
# ---------------------------------------------------------------------------


def test_api_session_reads_events(client, sample_session):
    resp = client.get(f"/api/session/{sample_session}")
    assert resp.status_code == 200
    events = resp.json()
    assert [e["type"] for e in events] == ["system", "assistant", "result"]


def test_api_session_skips_malformed_lines(client, _redirect_raw_sessions):
    filename = "malformed.jsonl"
    (_redirect_raw_sessions / filename).write_text(
        "not json\n" + json.dumps({"type": "result"}) + "\n"
    )
    resp = client.get(f"/api/session/{filename}")
    assert resp.status_code == 200
    assert resp.json() == [{"type": "result"}]


def test_api_session_missing_file_404(client):
    resp = client.get("/api/session/nope.jsonl")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Session file not found"


@pytest.mark.parametrize(
    "bad",
    ["..%2F..%2Fetc%2Fpasswd", "file.txt", "file%20name.jsonl", "sub%2Ffile.jsonl"],
)
def test_api_session_rejects_bad_filenames(client, bad):
    resp = client.get(f"/api/session/{bad}")
    assert resp.status_code in (400, 404)  # rejected before any read
    # a traversal/format reject is 400; a path that can't match is 404 — either
    # way it must never 200 or read outside the session dir
    assert resp.status_code != 200


# ---------------------------------------------------------------------------
# GET /session/{filename} — the page + its back link
# ---------------------------------------------------------------------------


def test_session_page_renders(client, sample_session):
    resp = client.get(f"/session/{sample_session}")
    assert resp.status_code == 200
    assert "session-timeline" in resp.text


def test_session_page_back_link_jobs(client, sample_session):
    resp = client.get(f"/session/{sample_session}?back=jobs")
    assert resp.status_code == 200
    assert 'href="/jobs"' in resp.text
    assert "Back to Jobs" in resp.text


def test_session_page_back_link_defaults_to_bot(client, sample_session):
    resp = client.get(f"/session/{sample_session}")
    assert resp.status_code == 200
    assert 'href="/bot/logs"' in resp.text


# ---------------------------------------------------------------------------
# Filename validation (direct)
# ---------------------------------------------------------------------------


class TestFilenameValidation:
    def test_valid_filename(self):
        sessions_routes._validate_session_filename(
            "2026-02-06_12-00-00-discord-sess-abc.jsonl"
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "../../etc/passwd",
            "subdir/file.jsonl",
            "subdir\\file.jsonl",
            "file.txt",
            "",
            "file name.jsonl",
        ],
    )
    def test_rejects_bad(self, bad):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            sessions_routes._validate_session_filename(bad)
        assert exc.value.status_code == 400

    def test_accepts_hyphens_underscores(self):
        sessions_routes._validate_session_filename(
            "2026-02-06_12-00-00-test-no-session.jsonl"
        )
