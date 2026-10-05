"""E2E for the workspace restore offer, in a real browser on throwaway Merlins.

Each server starts with a ``latest.json`` snapshot already in its home and an
empty tmux server, which is the state right after a reboot. The startup freeze
turns it into a restore offer, every page shows the banner, Restore rebuilds the
sessions in the server's private tmux, Dismiss drops the offer. A server whose
snapshot is fully live shows nothing.

Run: uv run scripts.py test-e2e   (or pytest tests/e2e/test_workspace_restore.py)
Requires: chromium, tmux.
"""

import json
import shutil
import subprocess

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import expect, sync_playwright

from conftest import start_merlin, stop_merlin  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


def seed(dirs: dict[str, str]):
    """A ``prepare`` callback writing a snapshot of one-window sessions."""

    def prepare(home):
        ws = home / "data" / "workspace"
        ws.mkdir(parents=True)
        snapshot = {
            "version": 1,
            "saved_at": 1791200000.0,
            "sessions": [
                {
                    "name": name,
                    "active_window": 1,
                    "windows": [
                        {
                            "index": 1,
                            "name": f"{name}-win",
                            "automatic_rename": False,
                            "layout": "",
                            "options": {"@agent_sid": f"sid-{name}"},
                            "panes": [{"index": 0, "cwd": cwd, "active": True}],
                        }
                    ],
                }
                for name, cwd in dirs.items()
            ],
        }
        (ws / "latest.json").write_text(json.dumps(snapshot))

    return prepare


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1100, "height": 720})
    pg = ctx.new_page()
    yield pg
    ctx.close()


@pytest.fixture
def seeded(tmp_path_factory):
    servers = []

    def make(names):
        dirs = {}
        for n in names:
            d = tmp_path_factory.mktemp(f"proj-{n}")
            dirs[n] = str(d)
        server = start_merlin(
            tmp_path_factory, prepare=seed(dirs), name="ws", ready="/files"
        )
        servers.append(server)
        return server, dirs

    yield make
    for s in servers:
        stop_merlin(s)


def tmux_sessions(server) -> list[str]:
    r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        env=server.env,
        capture_output=True,
        text=True,
        check=False,
    )
    return sorted(r.stdout.split())


def test_restore_rebuilds_the_missing_sessions(page, seeded):
    server, dirs = seeded(["alpha", "beta"])
    page.goto(f"{server.url}/files")
    banner = page.locator("#workspace-restore")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("2 sessions, saved")
    expect(banner).to_contain_text("alpha, beta")

    banner.get_by_role("button", name="Restore").click()
    expect(banner).to_contain_text("Restored 2 sessions")
    assert tmux_sessions(server) == ["alpha", "beta"]
    r = subprocess.run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            "=alpha:1",
            "#{window_name}\t#{pane_current_path}\t#{@agent_sid}",
        ],
        env=server.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.stdout.strip().split("\t") == ["alpha-win", dirs["alpha"], "sid-alpha"]

    # The offer is gone: the next page load shows nothing.
    page.goto(f"{server.url}/files")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#workspace-restore")).to_be_hidden()


def test_dismiss_drops_the_offer(page, seeded):
    server, _ = seeded(["gamma"])
    page.goto(f"{server.url}/terminal")
    banner = page.locator("#workspace-restore")
    expect(banner).to_be_visible()
    banner.get_by_role("button", name="Dismiss").click()
    expect(banner).to_be_hidden()
    assert "gamma" not in tmux_sessions(server)
    assert not (server.home / "data" / "workspace" / "pending-restore.json").exists()
    page.goto(f"{server.url}/files")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#workspace-restore")).to_be_hidden()


def test_no_offer_without_a_snapshot(page, server):
    page.goto(f"{server}/files")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#workspace-restore")).to_be_hidden()
