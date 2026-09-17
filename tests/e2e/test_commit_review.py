"""E2E checks for saved reviews and viewed files (commit-review epic, M2).

The repository is built here in a temp dir. The Merlin under test is the
shared throwaway fixture on its own home, so the reviews store lands under
that home and never under ~/.merlin. Every check runs at phone (390 px) and
desktop widths.

Run with: uv run scripts.py test-e2e
"""

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

VIEWPORTS = {"phone": (390, 844), "desktop": (1200, 800)}
REVIEW_URL = re.compile(r"^/commits/reviews/([0-9a-f]{8})$")


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00")
    env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"]
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """main: root -> second. feature/x from second: feat1 -> feat2 (two
    files). main then moves on. Checked out on feature/x."""
    root = tmp_path / "review-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")
    (root / "a.txt").write_text("alpha\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "Root commit")
    (root / "a.txt").write_text("alpha\nbeta\n")
    _git(root, "commit", "-q", "-am", "Second commit")
    _git(root, "checkout", "-q", "-b", "feature/x")
    (root / "feat.py").write_text("".join(f"print({i})\n" for i in range(1, 41)))
    (root / "other.py").write_text("x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "Feature one")
    (root / "other.py").write_text("x = 1\ny = 2\n")
    _git(root, "commit", "-q", "-am", "Feature two")
    _git(root, "checkout", "-q", "main")
    (root / "c.txt").write_text("c\n")
    _git(root, "add", "c.txt")
    _git(root, "commit", "-q", "-m", "Third commit")
    _git(root, "checkout", "-q", "feature/x")
    return root


@pytest.fixture(scope="module")
def playwright_instance():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright_instance):
    b = playwright_instance.firefox.launch(headless=True)
    yield b
    b.close()


@pytest.fixture(params=list(VIEWPORTS), ids=list(VIEWPORTS))
def page(request, browser):
    w, h = VIEWPORTS[request.param]
    ctx = browser.new_context(viewport={"width": w, "height": h})
    pg = ctx.new_page()
    yield pg
    ctx.close()


def _open_branch_comparison(page, server, repo):
    page.goto(
        f"{server}/commits/compare?repo={repo}&base=main&head=feature/x&mergebase=1"
    )
    page.wait_for_selector(".diff-file-section")


def _review_id(page) -> str:
    m = REVIEW_URL.match(urlparse(page.url).path)
    assert m, page.url
    return m.group(1)


def test_first_tick_creates_a_review_and_collapses_the_file(page, server, repo):
    # Start from the list so history has somewhere to go back to
    page.goto(f"{server}/commits?repo={repo}")
    page.wait_for_selector(".commit-item")
    _open_branch_comparison(page, server, repo)
    assert page.query_selector("#save-review-btn").is_visible()
    assert not page.query_selector("#review-chrome").is_visible()
    assert page.inner_text("#file-list-label") == "2 files changed"
    # Tick feat.py from its diff header
    page.check("#diff-file-feat\\.py .viewed-check input")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    review_id = _review_id(page)
    assert page.query_selector("#review-chrome").is_visible()
    assert not page.query_selector("#save-review-btn").is_visible()
    assert page.input_value("#review-title") == "feature/x vs main"
    assert page.text_content("#review-status") == "Open"
    assert "feature/x" in page.inner_text("#review-refs")
    page.wait_for_function(
        "() => document.querySelector('#file-list-label').textContent === '1 of 2 viewed'"
    )
    section = page.query_selector("#diff-file-feat\\.py")
    assert "viewed" in section.get_attribute("class")
    assert not section.query_selector(".diff-table-scroll").is_visible()
    other = page.query_selector("#diff-file-other\\.py")
    assert other.query_selector(".diff-table-scroll").is_visible()
    # Tapping the header of a viewed file expands it again
    section.query_selector(".diff-file-path").click()
    assert section.query_selector(".diff-table-scroll").is_visible()
    # Back returns to the list (the URL was replaced, not pushed)
    page.go_back()
    page.wait_for_selector(".commit-item")
    assert urlparse(page.url).path == "/commits"
    # The list page shows the review
    page.wait_for_selector(".review-item")
    items = page.query_selector_all(".review-item")
    assert [i.get_attribute("data-id") for i in items] == [review_id]
    assert "feature/x vs main" in items[0].inner_text()
    items[0].click()
    page.wait_for_selector("#review-chrome")
    assert _review_id(page) == review_id
    page.wait_for_function(
        "() => document.querySelector('#file-list-label').textContent === '1 of 2 viewed'"
    )


def test_save_as_review_and_rename_close_reopen(page, server, repo):
    _open_branch_comparison(page, server, repo)
    page.click("#save-review-btn")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    review_id = _review_id(page)
    assert page.inner_text("#file-list-label") == "0 of 2 viewed"
    page.fill("#review-title", "My branch review")
    page.press("#review-title", "Enter")
    page.wait_for_timeout(300)
    page.click("#review-status-btn")
    page.wait_for_function(
        "() => document.querySelector('#review-status').textContent === 'Closed'"
    )
    page.reload()
    page.wait_for_selector("#review-chrome")
    assert page.input_value("#review-title") == "My branch review"
    assert page.text_content("#review-status") == "Closed"
    assert page.inner_text("#review-status-btn") == "Reopen"
    # The list groups it under Closed
    page.goto(f"{server}/commits?repo={repo}")
    page.wait_for_selector("#reviews-closed-btn", state="visible")
    assert page.query_selector_all("#reviews-open .review-item") == []
    assert not page.query_selector("#reviews-closed").is_visible()
    page.click("#reviews-closed-btn")
    closed = page.query_selector_all("#reviews-closed .review-item")
    assert [i.get_attribute("data-id") for i in closed] == [review_id]
    closed[0].click()
    page.wait_for_selector("#review-chrome")
    page.click("#review-status-btn")
    page.wait_for_function(
        "() => document.querySelector('#review-status').textContent === 'Open'"
    )


def test_amending_a_viewed_file_clears_the_tick(page, server, repo):
    _open_branch_comparison(page, server, repo)
    page.check("#diff-file-other\\.py .viewed-check input")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    page.wait_for_function(
        "() => document.querySelector('#file-list-label').textContent === '1 of 2 viewed'"
    )
    # A new commit on the branch changes other.py's patch
    (repo / "other.py").write_text("x = 1\ny = 2\nz = 3\n")
    _git(repo, "commit", "-q", "-am", "Feature three")
    page.reload()
    page.wait_for_selector("#review-chrome")
    page.wait_for_function(
        "() => document.querySelector('#file-list-label').textContent === '0 of 2 viewed'"
    )
    page.click("#file-list-toggle-btn")
    item = page.query_selector(".file-list-item[data-path='other.py']")
    assert not item.query_selector(".viewed-check input").is_checked()
    assert item.query_selector(".file-changed-marker").is_visible()
    feat = page.query_selector(".file-list-item[data-path='feat.py']")
    assert not feat.query_selector(".file-changed-marker").is_visible()
    assert page.query_selector("#review-new-commits").is_visible()
    assert "1 new commit since you last looked" in page.inner_text(
        "#review-new-commits"
    )
    # The section is open again
    assert page.query_selector("#diff-file-other\\.py .diff-table-scroll").is_visible()
    # A second look reports nothing new
    page.reload()
    page.wait_for_selector("#review-chrome")
    page.wait_for_timeout(300)
    assert not page.query_selector("#review-new-commits").is_visible()


def test_poll_picks_up_a_change_made_elsewhere(page, server, repo):
    _open_branch_comparison(page, server, repo)
    page.click("#save-review-btn")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    review_id = _review_id(page)
    # Another client renames the review: the page follows within the poll
    page.request.patch(
        f"{server}/api/commits/reviews/{review_id}",
        data={"title": "Renamed elsewhere"},
    )
    page.wait_for_function(
        "() => document.querySelector('#review-title').value === 'Renamed elsewhere'",
        timeout=15000,
    )


def test_copy_for_agent(page, server, repo):
    _open_branch_comparison(page, server, repo)
    page.click("#save-review-btn")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    review_id = _review_id(page)
    page.click("#review-copy-btn")
    label = page.inner_text("#review-copy-btn span")
    # Headless Firefox may refuse the clipboard: the fallback shows the command
    assert label in ("Copied", f"merlin review show {review_id}")


def _make_other_repo(tmp_path) -> Path:
    other = tmp_path / "other-repo"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "Tester")
    (other / "feat.py").write_text("OTHER\n")
    _git(other, "add", ".")
    _git(other, "commit", "-q", "-m", "Other root")
    (other / "feat.py").write_text("OTHER\nMORE\n")
    _git(other, "commit", "-q", "-am", "Other second")
    _git(other, "checkout", "-q", "-b", "feature/x")
    return other


def test_bare_review_url_binds_to_the_review_repository(page, server, repo, tmp_path):
    """A review id is enough: with no ?repo=, a saved repo or a conflicting
    ?repo=, the page shows the review's own repository."""
    other = _make_other_repo(tmp_path)
    resp = page.request.post(
        f"{server}/api/commits/reviews",
        data={
            "repo": str(repo),
            "base": "main",
            "head": "feature/x",
            "mergebase": True,
        },
    )
    review_id = resp.json()["id"]
    # The browser last used the other repository
    page.goto(f"{server}/commits?repo={other}")
    page.wait_for_selector(".commit-item")
    # Bare URL, no repo at all
    page.goto(f"{server}/commits/reviews/{review_id}")
    page.wait_for_selector("#review-chrome")
    page.wait_for_selector(".diff-file-section")
    paths = [el.inner_text() for el in page.query_selector_all(".diff-file-path")]
    assert paths == ["feat.py", "other.py"]
    assert "OTHER" not in page.inner_text("#diff-content")
    assert "print(1)" in page.inner_text("#diff-content")
    assert page.inner_text("#repo-path").endswith("review-repo")
    # A conflicting ?repo= is overridden by the record
    page.goto(f"{server}/commits/reviews/{review_id}?repo={other}")
    page.wait_for_selector("#review-chrome")
    page.wait_for_selector(".diff-file-section")
    assert "OTHER" not in page.inner_text("#diff-content")
    assert "print(1)" in page.inner_text("#diff-content")
    assert f"repo={other}" not in page.url
    page.click("#diff-file-feat\\.py .full-file-btn")
    page.wait_for_selector(".file-table")
    assert "print(1)" in page.inner_text("#file-content")
    assert "OTHER" not in page.inner_text("#file-content")
    # Back to the list: the review's repository is now the page's
    page.click("#file-back-btn")
    page.wait_for_selector("#review-chrome")
    page.click("#diff-back-btn")
    page.wait_for_selector(".commit-item")
    assert "Feature two" in page.inner_text("#commit-list")


def test_two_quick_ticks_create_one_review(page, server, repo):
    _open_branch_comparison(page, server, repo)
    page.evaluate(
        """() => {
            const boxes = document.querySelectorAll('#diff-content .viewed-check input');
            boxes[0].click();
            boxes[1].click();
        }"""
    )
    page.wait_for_function(
        "() => document.querySelector('#file-list-label').textContent === '2 of 2 viewed'"
    )
    review_id = _review_id(page)
    listed = page.request.get(f"{server}/api/commits/reviews?repo={repo}").json()
    assert [r["id"] for r in listed] == [review_id]
    record = page.request.get(f"{server}/api/commits/reviews/{review_id}?since=").json()
    assert set(record["review"]["files"]) == {"feat.py", "other.py"}
    page.reload()
    page.wait_for_function(
        "() => document.querySelector('#file-list-label').textContent === '2 of 2 viewed'"
    )


def test_phone_review_controls_are_44px(page, server, repo):
    if page.viewport_size["width"] >= 768:
        pytest.skip("phone only")
    _open_branch_comparison(page, server, repo)
    page.click("#save-review-btn")
    page.wait_for_selector("#review-chrome")
    for sel in (
        "#review-title",
        "#review-copy-btn",
        "#review-status-btn",
        "#diff-file-feat\\.py .viewed-check",
    ):
        box = page.query_selector(sel).bounding_box()
        assert box["height"] >= 44, (sel, box)
    page.click("#review-status-btn")
    page.wait_for_function(
        "() => document.querySelector('#review-status').textContent === 'Closed'"
    )
    page.goto(f"{server}/commits?repo={repo}")
    page.wait_for_selector("#reviews-closed-btn", state="visible")
    box = page.query_selector("#reviews-closed-btn").bounding_box()
    assert box["height"] >= 44, box


def test_review_file_deep_link(page, server, repo):
    _open_branch_comparison(page, server, repo)
    page.click("#save-review-btn")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    review_id = _review_id(page)
    page.goto(f"{server}/commits/reviews/{review_id}/file/other.py?repo={repo}")
    page.wait_for_selector(".file-table")
    assert page.query_selector_all(".file-table tbody tr")
    page.click("#file-back-btn")
    page.wait_for_selector("#review-chrome")
    assert _review_id(page) == review_id
