"""E2E checks for the Commits page comparisons (commit-review epic, M1).

The repository under test is built here in a temp dir (commits, a branch
whose base moved on, a dirty tree). The Merlin under test is the shared
throwaway fixture on its own home. Every check runs at phone (390 px) and
desktop widths.

Run with: uv run scripts.py test-e2e
"""

import os
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright

VIEWPORTS = {"phone": (390, 844), "desktop": (1200, 800)}


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00")
    env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"]
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    """main: root -> second -> third. feature/x from second: feat1 -> feat2.
    Checked out on feature/x with a staged, an unstaged and an untracked
    change. The root commit carries a long file so a diff scrolls."""
    root = tmp_path_factory.mktemp("compare-repo")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")
    (root / "long.py").write_text("".join(f"line {i}\n" for i in range(1, 121)))
    (root / "a.txt").write_text("alpha\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "Root commit")
    (root / "a.txt").write_text("alpha\nbeta\n")
    _git(root, "commit", "-q", "-am", "Second commit")
    _git(root, "checkout", "-q", "-b", "feature/x")
    (root / "feat.py").write_text("".join(f"print({i})\n" for i in range(1, 101)))
    _git(root, "add", "feat.py")
    _git(root, "commit", "-q", "-m", "Feature one")
    (root / "feat.py").write_text(
        "".join(f"print({i})\n" for i in range(1, 101)) + "print('end')\n"
    )
    _git(root, "commit", "-q", "-am", "Feature two")
    _git(root, "checkout", "-q", "main")
    (root / "c.txt").write_text("c\n")
    _git(root, "add", "c.txt")
    _git(root, "commit", "-q", "-m", "Third commit")
    _git(root, "checkout", "-q", "feature/x")
    (root / "a.txt").write_text("alpha\nbeta\nstaged\n")
    _git(root, "add", "a.txt")
    (root / "feat.py").write_text((root / "feat.py").read_text() + "print('wip')\n")
    (root / "untracked.txt").write_text("u1\nu2\n")
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


def _open_list(page, server, repo):
    page.goto(f"{server}/commits?repo={repo}")
    page.wait_for_selector(".commit-item")


def _query(page) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(page.url).query).items()}


def test_compare_sheet_opens_branch_comparison(page, server, repo):
    _open_list(page, server, repo)
    page.click("#compare-btn")
    page.wait_for_selector("#compare-sheet", state="visible")
    page.wait_for_selector(".compare-ref-item")
    assert page.input_value("#compare-head") == "feature/x"
    assert page.input_value("#compare-base") == "main"
    assert page.is_checked("#compare-mergebase")
    # The list follows the focused field and filters by its text
    names = [el.inner_text() for el in page.query_selector_all(".picker-item-name")]
    assert names == ["feature/x"]
    assert "head" in page.inner_text("#compare-ref-hint").lower()
    page.focus("#compare-base")
    page.wait_for_timeout(100)
    names = [el.inner_text() for el in page.query_selector_all(".picker-item-name")]
    assert names == ["main"]
    page.fill("#compare-base", "")
    names = [el.inner_text() for el in page.query_selector_all(".picker-item-name")]
    assert set(names) == {"main", "feature/x"}
    page.click(".compare-ref-item:has-text('main')")
    assert page.input_value("#compare-base") == "main"
    page.click("#compare-go-btn")
    page.wait_for_selector(".diff-file-section")
    assert urlparse(page.url).path == "/commits/compare"
    q = _query(page)
    assert q["base"] == "main" and q["head"] == "feature/x" and q["mergebase"] == "1"
    header = page.inner_text("#diff-meta")
    assert "feature/x vs main" in header
    assert "main" in header and "feature/x" in header
    assert "merge base" in header
    assert "2 commits" in header
    # Only the branch's own file, not main's later c.txt
    paths = [el.inner_text() for el in page.query_selector_all(".diff-file-path")]
    assert paths == ["feat.py"]
    page.click("#compare-commits-btn")
    items = page.query_selector_all(".compare-commit-item")
    assert [i.inner_text() for i in items] and len(items) == 2
    assert "Feature two" in items[0].inner_text()


def test_select_mode_opens_range(page, server, repo):
    _open_list(page, server, repo)
    assert page.query_selector("#select-bar").is_visible() is False
    page.click("#select-toggle")
    assert page.query_selector("#select-bar").is_visible()
    rows = page.query_selector_all(".commit-item")
    hashes = [r.get_attribute("data-hash") for r in rows]
    rows[0].click()
    assert page.inner_text("#select-summary") == "1 commit selected"
    rows[2].click()
    assert page.inner_text("#select-summary") == "3 commits selected"
    # Order of taps is irrelevant: tapping the newest again clears it, then
    # picking it again from the other end gives the same range.
    rows[0].click()
    assert page.inner_text("#select-summary") == "1 commit selected"
    rows[0].click()
    assert page.inner_text("#select-summary") == "3 commits selected"
    in_range = page.query_selector_all(".commit-item.in-range")
    assert len(in_range) == 3
    page.click("#select-compare-btn")
    page.wait_for_selector(".diff-file-section")
    q = _query(page)
    assert q["head"] == hashes[0]
    assert q["base"] == hashes[2] + "^"
    header = page.inner_text("#diff-meta")
    assert "3 commits" in header
    assert "Range" in header
    # Back returns to the list, out of selection mode state in the URL
    page.click("#diff-back-btn")
    page.wait_for_selector(".commit-item")
    assert urlparse(page.url).path == "/commits"


def test_worktree_row_on_dirty_tree(page, server, repo):
    _open_list(page, server, repo)
    row = page.wait_for_selector("#worktree-row", state="visible")
    text = row.inner_text()
    assert "Working tree" in text
    assert "3 files" in text  # a.txt staged, feat.py unstaged, untracked.txt
    row.click()
    page.wait_for_selector(".diff-file-section")
    assert urlparse(page.url).path == "/commits/compare"
    assert _query(page)["worktree"] == "1"
    header = page.inner_text("#diff-meta")
    assert "Working tree" in header
    paths = [el.inner_text() for el in page.query_selector_all(".diff-file-path")]
    assert paths == ["a.txt", "feat.py", "untracked.txt"]
    # The untracked file opens as a full file read from disk
    page.click(".diff-file-section:last-child .full-file-btn")
    page.wait_for_selector(".file-table")
    assert urlparse(page.url).path == "/commits/compare/file/untracked.txt"
    assert (
        page.query_selector_all(".file-line-added")
        and len(page.query_selector_all(".file-table tbody tr")) == 2
    )


def test_single_commit_view_still_works(page, server, repo):
    _open_list(page, server, repo)
    rows = page.query_selector_all(".commit-item")
    rows[-1].click()  # the root commit
    page.wait_for_selector(".diff-file-section")
    assert urlparse(page.url).path.startswith("/commits/")
    assert "Root commit" in page.inner_text("#diff-meta")
    paths = [el.inner_text() for el in page.query_selector_all(".diff-file-path")]
    assert paths == ["a.txt", "long.py"]


def test_file_header_is_sticky(page, server, repo):
    """The first file's header stays pinned to the top of the viewport while
    its diff scrolls, measured after a scroll rather than read from CSS."""
    page.goto(
        f"{server}/commits/compare?repo={repo}&base=main&head=feature/x&mergebase=1"
    )
    page.wait_for_selector(".diff-file-section")
    before = page.evaluate(
        "() => document.querySelector('.diff-file-header').getBoundingClientRect().top"
    )
    assert before > 0
    page.evaluate("window.scrollBy(0, 400)")
    page.wait_for_timeout(200)
    info = page.evaluate(
        """() => {
            const sec = document.querySelector('.diff-file-section');
            const hdr = sec.querySelector('.diff-file-header');
            return {section: sec.getBoundingClientRect().top,
                    header: hdr.getBoundingClientRect().top,
                    bottom: sec.getBoundingClientRect().bottom,
                    scrollY: window.scrollY};
        }"""
    )
    assert info["scrollY"] >= 300
    assert info["section"] < 0, info
    assert info["bottom"] > 0, info
    assert abs(info["header"]) <= 1, info
    # No ancestor between the header and the window clips it
    overflows = page.evaluate(
        """() => {
            const out = [];
            let el = document.querySelector('.diff-file-header').parentElement;
            while (el && el !== document.body) {
                const o = getComputedStyle(el).overflow;
                if (o !== 'visible') out.push(el.className + ':' + o);
                el = el.parentElement;
            }
            return out;
        }"""
    )
    assert overflows == [], overflows


def test_bad_ref_shows_an_error_not_a_crash(page, server, repo):
    page.goto(f"{server}/commits/compare?repo={repo}&base=nope&head=feature/x")
    page.wait_for_selector(".diff-error")
    assert "Unknown ref" in page.inner_text("#diff-meta")
