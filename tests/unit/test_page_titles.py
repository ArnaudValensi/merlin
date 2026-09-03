"""Browser-title contract across the shared shell and dynamic pages."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import main as app_mod
from merlin_ext import register_template_globals, resolve_machine_name


ROOT = Path(__file__).resolve().parents[2]


def _title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    assert match is not None
    return match.group(1).strip()


@pytest.fixture
def client(monkeypatch):
    original_machine = resolve_machine_name()
    register_template_globals(machine_name="atlas")
    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.setattr(app_mod, "TMUX_AVAILABLE", True)
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    auth.configure("")
    with TestClient(app_mod.app) as test_client:
        yield test_client
    register_template_globals(machine_name=original_machine)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/login", "atlas · login"),
        ("/files", "atlas · files"),
        ("/terminal", "atlas · term"),
        ("/terminal/clipboard-test", "atlas · clipboard"),
        ("/commits", "atlas · commits"),
        ("/jobs", "atlas · jobs"),
        ("/extensions", "atlas · extensions"),
        ("/settings", "atlas · settings"),
    ],
)
def test_static_page_titles_are_machine_first(client, path, expected):
    response = client.get(path)
    assert response.status_code == 200
    assert _title(response.text) == expected


def test_managed_environment_slug_precedes_hostname():
    assert (
        resolve_machine_name(
            {"MERLIN_ENVIRONMENT_SLUG": " studio "}, hostname=lambda: "host"
        )
        == "studio"
    )


def test_self_hosted_instance_uses_trimmed_hostname():
    assert resolve_machine_name({}, hostname=lambda: " atlas.local \n") == "atlas.local"


def test_failed_hostname_lookup_degrades_to_no_machine():
    def unavailable():
        raise OSError("hostname unavailable")

    assert resolve_machine_name({}, hostname=unavailable) == ""


@pytest.mark.parametrize(
    ("relative_path", "integration"),
    [
        (
            "terminal/templates/terminal.html",
            "MerlinPageTitle.tmuxContext(ctl.name, ctl.window)",
        ),
        (
            "files/static/files.js",
            "MerlinPageTitle.pathContext(fsPath)",
        ),
        (
            "commits/static/commits.js",
            "MerlinPageTitle.pathContext(path)",
        ),
        (
            "notes/static/notes.js",
            "meta.title || MerlinPageTitle.pathContext(this._path)",
        ),
    ],
)
def test_dynamic_pages_use_the_shared_title_helper(relative_path, integration):
    assert integration in (ROOT / relative_path).read_text()
