"""E2E checks for comment threads and the agent CLI (commit-review epic, M3).

The repository is built here, the Merlin under test is the shared throwaway
fixture on its own home, and ``merlin review`` runs against that same home
(``MERLIN_HOME`` from the server's environment), never the real one. Every
check runs at phone (390 px) and desktop widths.

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

from conftest import ROOT

VIEWPORTS = {"phone": (390, 844), "desktop": (1200, 800)}
REVIEW_URL = re.compile(r"^/commits/reviews/([0-9a-f]{8})")


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00")
    env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"]
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "comments-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")
    (root / "a.txt").write_text("alpha\nbeta\ngamma\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "Root commit")
    _git(root, "checkout", "-q", "-b", "feature/x")
    (root / "a.txt").write_text("alpha\nBETA\ngamma\ndelta\n")
    (root / "feat.py").write_text("".join(f"print({i})\n" for i in range(1, 21)))
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "Feature one")
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


def merlin_review(server_env: dict, *args: str) -> str:
    """Run ``merlin review`` against the throwaway home of the server."""
    env = os.environ.copy()
    env["MERLIN_HOME"] = server_env["MERLIN_HOME"]
    env["MERLIN_DEV"] = "1"
    proc = subprocess.run(
        ["uv", "run", "cli.py", "review", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _open_branch_comparison(page, server, repo):
    page.goto(
        f"{server}/commits/compare?repo={repo}&base=main&head=feature/x&mergebase=1"
    )
    page.wait_for_selector(".diff-file-section")


def _review_id(page) -> str:
    m = REVIEW_URL.match(urlparse(page.url).path)
    assert m, page.url
    return m.group(1)


def _comment_on(page, section_sel, side, line, text):
    row = page.query_selector(
        f"{section_sel} tr[data-side='{side}'][data-line='{line}']"
    )
    assert row, (section_sel, side, line)
    row.query_selector_all(".diff-line-no")[-1].click()
    row.query_selector(".comment-add-btn").click()
    page.fill(f"{section_sel} .comment-composer-row textarea", text)
    page.click(f"{section_sel} .comment-composer-row .composer-submit")


def test_first_comment_creates_the_review_and_threads_render(page, server, repo):
    _open_branch_comparison(page, server, repo)
    assert page.query_selector("#review-comments").is_visible()
    _comment_on(page, "#diff-file-a\\.txt", "new", 2, "Why uppercase here?")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    review_id = _review_id(page)
    page.wait_for_selector("#diff-file-a\\.txt .comment-thread-row .thread")
    thread = page.query_selector("#diff-file-a\\.txt .comment-thread-row .thread")
    assert "Why uppercase here?" in thread.inner_text()
    assert thread.query_selector(".author-badge").inner_text().lower() == "you"
    # The thread sits right under its line
    prev = thread.evaluate("el => el.closest('tr').previousElementSibling.dataset.line")
    assert prev == "2"
    assert page.inner_text("#diff-file-a\\.txt .diff-file-count") == "1 open"
    # A comment on the deleted line lands on the old side
    _comment_on(page, "#diff-file-a\\.txt", "old", 2, "beta was fine")
    page.wait_for_function(
        "() => document.querySelectorAll('#diff-file-a\\\\.txt .comment-thread-row').length === 2"
    )
    assert page.inner_text("#diff-file-a\\.txt .diff-file-count") == "2 open"
    # A reply from the page
    page.fill(
        "#diff-file-a\\.txt .comment-thread-row .thread-reply-text >> nth=0",
        "Because shouting",
    )
    page.click("#diff-file-a\\.txt .comment-thread-row .thread-reply-btn >> nth=0")
    page.wait_for_selector("#diff-file-a\\.txt .thread-reply")
    assert "Because shouting" in page.inner_text("#diff-file-a\\.txt .thread-reply")
    # A review-wide comment
    page.click("#review-add-comment-btn")
    page.fill("#review-composer-text", "Overall: fine")
    page.click("#review-composer-submit")
    page.wait_for_selector("#review-threads .thread")
    assert "Overall: fine" in page.inner_text("#review-threads")
    # Everything survives a reload
    page.reload()
    page.wait_for_selector("#review-threads .thread")
    page.wait_for_function(
        "() => document.querySelectorAll('#diff-file-a\\\\.txt .comment-thread-row').length === 2"
    )
    assert _review_id(page) == review_id


def test_cli_resolve_shows_up_after_the_poll(page, server, repo, tmux_env):
    _open_branch_comparison(page, server, repo)
    _comment_on(page, "#diff-file-feat\\.py", "new", 3, "Drop this print")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    review_id = _review_id(page)
    page.wait_for_selector("#diff-file-feat\\.py .thread.open")
    shown = merlin_review(tmux_env, "show", review_id)
    assert "Drop this print" in shown and "feat.py:3 (new)" in shown
    assert "> print(3)" in shown
    thread_id = re.search(r"^### ([0-9a-f]{8}) · feat.py:3", shown, re.M).group(1)
    merlin_review(
        tmux_env,
        "reply",
        review_id,
        thread_id,
        "Removed",
        "in",
        "the",
        "next",
        "commit.",
    )
    merlin_review(tmux_env, "resolve", review_id, thread_id, "-m", "Done.")
    # The page follows within the poll: the thread folds as resolved
    page.wait_for_selector("#diff-file-feat\\.py .thread.resolved", timeout=15000)
    label = page.inner_text("#diff-file-feat\\.py .thread-resolved-label")
    assert label == "Resolved · 2 replies"
    assert page.inner_text("#diff-file-feat\\.py .diff-file-count") == ""
    page.click("#diff-file-feat\\.py .thread-expand")
    text = page.inner_text("#diff-file-feat\\.py .thread.resolved")
    assert "Removed in the next commit." in text and "Done." in text
    assert "agent" in text.lower()
    # The agent's reply and resolve arrived after this visit began: the
    # "since your last visit" panel lists them live, the thread is unread.
    activity = page.inner_text("#review-activity")
    assert "agent replied on feat.py:3" in activity
    assert "agent resolved feat.py:3" in activity
    assert "Removed in the next commit." in activity
    assert (
        page.query_selector("#diff-file-feat\\.py .thread.unread .thread-unread")
        is not None
    )
    # Tapping an activity row scrolls to the thread
    page.click("#review-activity .activity-resolved")
    page.wait_for_timeout(300)
    assert page.query_selector("#diff-file-feat\\.py .thread").is_visible()
    # A fresh visit measures from the previous one: the agent's actions
    # still show (they came after that visit), then a second visit clears them.
    page.reload()
    page.wait_for_selector("#review-chrome")
    page.wait_for_timeout(300)
    assert "agent resolved feat.py:3" in page.inner_text("#review-activity")
    page.reload()
    page.wait_for_selector("#review-chrome")
    page.wait_for_timeout(300)
    assert not page.query_selector("#review-activity").is_visible()
    # Reopen from the page, the CLI sees it open again
    page.click("#diff-file-feat\\.py .thread-expand")
    page.click("#diff-file-feat\\.py .thread-reopen-btn")
    page.wait_for_selector("#diff-file-feat\\.py .thread.open")
    shown = merlin_review(tmux_env, "show", review_id)
    assert "## Open threads (1)" in shown


def test_clean_tree_disables_the_worktree_shortcut(page, server, repo):
    """This repository has no uncommitted change: no pinned row, and the
    sheet's shortcut is disabled and says so."""
    page.goto(f"{server}/commits?repo={repo}")
    page.wait_for_selector(".commit-item")
    page.wait_for_timeout(300)
    assert not page.query_selector("#worktree-row").is_visible()
    page.click("#compare-btn")
    page.wait_for_selector(".compare-ref-item")
    page.wait_for_function(
        "() => document.querySelector('#compare-worktree-btn').disabled"
    )
    if page.viewport_size["width"] >= 768:
        assert "no uncommitted changes" in page.inner_text("#compare-worktree-hint")
    # A worktree comparison opened by URL on a clean tree says so too
    page.goto(f"{server}/commits/compare?repo={repo}&worktree=1")
    page.wait_for_selector("#diff-content .empty-state")
    assert "working tree is clean" in page.inner_text("#diff-content").lower()


def test_outdated_thread_moves_to_the_top_of_its_file(page, server, repo):
    _open_branch_comparison(page, server, repo)
    _comment_on(page, "#diff-file-a\\.txt", "new", 4, "delta?")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    page.wait_for_selector("#diff-file-a\\.txt .comment-thread-row .thread")
    # Rewrite the line: the thread is outdated, quoted at the top of its file
    (repo / "a.txt").write_text("alpha\nBETA\ngamma\nDELTA\n")
    _git(repo, "commit", "-q", "-am", "Rewrite delta")
    page.reload()
    page.wait_for_selector("#diff-file-a\\.txt .file-threads-top .thread")
    top = page.query_selector("#diff-file-a\\.txt .file-threads-top .thread")
    assert top.query_selector(".thread-state.outdated") is not None
    assert "delta" in top.query_selector(".thread-quote").inner_text()
    assert page.query_selector_all("#diff-file-a\\.txt .comment-thread-row") == []
    # Still answerable
    top.query_selector(".thread-reply-text").fill("Still relevant")
    top.query_selector(".thread-reply-btn").click()
    page.wait_for_selector("#diff-file-a\\.txt .file-threads-top .thread-reply")
    # A shifted line is followed instead
    (repo / "feat.py").write_text(
        "print(0)\n" + "".join(f"print({i})\n" for i in range(1, 21))
    )
    _git(repo, "commit", "-q", "-am", "Shift feat")
    _comment_on(page, "#diff-file-feat\\.py", "new", 3, "line three")  # print(2) now
    page.wait_for_selector("#diff-file-feat\\.py .comment-thread-row .thread")
    (repo / "feat.py").write_text(
        "print(-1)\nprint(0)\n" + "".join(f"print({i})\n" for i in range(1, 21))
    )
    _git(repo, "commit", "-q", "-am", "Shift feat again")
    page.reload()
    page.wait_for_selector("#diff-file-feat\\.py .comment-thread-row .thread")
    moved = page.query_selector("#diff-file-feat\\.py .comment-thread-row .thread")
    assert moved.query_selector(".thread-state.moved") is not None
    prev = moved.evaluate("el => el.closest('tr').previousElementSibling.dataset.line")
    assert prev == "4"


def test_comment_from_the_full_file_view(page, server, repo):
    _open_branch_comparison(page, server, repo)
    page.click("#diff-file-feat\\.py .full-file-btn")
    page.wait_for_selector(".file-table")
    row = page.query_selector("#file-line-5")
    row.query_selector(".file-line-no").click()
    row.query_selector(".comment-add-btn").click()
    page.fill(".comment-composer-row textarea", "From the file view")
    page.click(".comment-composer-row .composer-submit")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    assert urlparse(page.url).path.endswith("/file/feat.py")
    page.wait_for_selector("#file-content .comment-thread-row .thread")
    prev = page.evaluate(
        "() => document.querySelector('#file-content .comment-thread-row').previousElementSibling.id"
    )
    assert prev == "file-line-5"
    # Back to the review: the thread is under print(5) in the diff
    page.click("#file-back-btn")
    page.wait_for_selector("#diff-file-feat\\.py .comment-thread-row .thread")
    prev = page.evaluate(
        "() => document.querySelector('#diff-file-feat\\\\.py .comment-thread-row').previousElementSibling.dataset.line"
    )
    assert prev == "5"


def test_review_file_deep_link_carries_anchor_states(page, server, repo):
    """A direct /commits/reviews/<id>/file/<path> URL loads the review in
    full, so a moved thread follows its line and an outdated one sits at the
    top with its quote and label."""
    _open_branch_comparison(page, server, repo)
    _comment_on(page, "#diff-file-feat\\.py", "new", 3, "will move")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    review_id = _review_id(page)
    page.wait_for_selector("#diff-file-feat\\.py .comment-thread-row .thread")
    _comment_on(page, "#diff-file-feat\\.py", "new", 10, "will be outdated")
    page.wait_for_function(
        "() => document.querySelectorAll('#diff-file-feat\\\\.py .comment-thread-row').length === 2"
    )
    # Shift the file by one line and rewrite line 10's text
    lines = ["print(0)"] + [f"print({i})" for i in range(1, 21)]
    lines[10] = "print('ten')"  # was print(10)
    (repo / "feat.py").write_text("".join(line + "\n" for line in lines))
    _git(repo, "commit", "-q", "-am", "Shift and rewrite")
    # A fresh page, straight to the file URL
    page.goto(f"{server}/commits/reviews/{review_id}/file/feat.py")
    page.wait_for_selector(".file-table")
    page.wait_for_selector("#file-content .comment-thread-row .thread")
    moved = page.query_selector("#file-content .comment-thread-row .thread")
    assert "will move" in moved.inner_text()
    assert moved.query_selector(".thread-state.moved") is not None
    prev = page.evaluate(
        "() => document.querySelector('#file-content .comment-thread-row').previousElementSibling.id"
    )
    assert prev == "file-line-4"  # print(3) is now line 4
    page.wait_for_selector("#file-threads-top .thread")
    top = page.query_selector("#file-threads-top .thread")
    assert "will be outdated" in top.inner_text()
    assert top.query_selector(".thread-state.outdated") is not None
    assert "print(10)" in top.query_selector(".thread-quote").inner_text()
    assert len(page.query_selector_all("#file-content .comment-thread-row")) == 1


def test_file_deep_link_is_not_a_visit(page, server, repo, tmux_env):
    """Opening a review's file by URL loads the anchors but does not consume
    the "since your last visit" panel: back on the review page, the agent's
    reply is still listed."""
    _open_branch_comparison(page, server, repo)
    _comment_on(page, "#diff-file-feat\\.py", "new", 3, "Drop this print")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    review_id = _review_id(page)
    page.wait_for_selector("#diff-file-feat\\.py .thread.open")
    shown = merlin_review(tmux_env, "show", review_id)
    thread_id = re.search(r"^### ([0-9a-f]{8}) · feat.py:3", shown, re.M).group(1)
    # The user leaves, the agent replies
    page.goto(f"{server}/commits?repo={repo}")
    page.wait_for_selector(".commit-item")
    merlin_review(tmux_env, "reply", review_id, thread_id, "Gone.")
    # A file deep link, then the review page
    page.goto(f"{server}/commits/reviews/{review_id}/file/feat.py")
    page.wait_for_selector("#file-content .comment-thread-row .thread")
    assert "Gone." in page.inner_text("#file-content .comment-thread-row .thread")
    page.goto(f"{server}/commits/reviews/{review_id}")
    page.wait_for_selector("#review-activity", state="visible")
    assert "agent replied on feat.py:3" in page.inner_text("#review-activity")


def test_threads_share_one_size_everywhere(page, server, repo):
    """The inline thread under a line and the review-wide thread in the
    panel render the same 13 px, the dashboard's text size."""
    _open_branch_comparison(page, server, repo)
    _comment_on(page, "#diff-file-feat\\.py", "new", 3, "inline")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    page.wait_for_selector("#diff-file-feat\\.py .comment-thread-row .thread")
    page.click("#review-add-comment-btn")
    page.fill("#review-composer-text", "review-wide")
    page.click("#review-composer-submit")
    page.wait_for_selector("#review-threads .thread")
    sizes = page.evaluate(
        """() => ['#diff-file-feat\\\\.py .comment-thread-row .thread',
                  '#diff-file-feat\\\\.py .comment-thread-row .thread-body',
                  '#review-threads .thread',
                  '#review-threads .thread-body',
                  '#review-threads .thread-textarea'].map(
            s => getComputedStyle(document.querySelector(s)).fontSize)"""
    )
    assert sizes == ["13px"] * 5, sizes
    fonts = page.evaluate(
        """() => ['#diff-file-feat\\\\.py .comment-thread-row .thread-textarea',
                  '#review-threads .thread-btn'].map(
            s => getComputedStyle(document.querySelector(s)).fontFamily)"""
    )
    assert all("Geist Mono" not in f for f in fonts), fonts


def test_poll_runs_in_the_full_file_view(page, server, repo, tmux_env):
    """A review created from the full-file view, and a direct file URL, both
    pick up an agent's reply and resolve through the poll."""
    _open_branch_comparison(page, server, repo)
    page.click("#diff-file-feat\\.py .full-file-btn")
    page.wait_for_selector(".file-table")
    row = page.query_selector("#file-line-7")
    row.query_selector(".file-line-no").click()
    row.query_selector(".comment-add-btn").click()
    page.fill(".comment-composer-row textarea", "Seven")
    page.click(".comment-composer-row .composer-submit")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    review_id = _review_id(page)
    page.wait_for_selector("#file-content .thread.open")
    shown = merlin_review(tmux_env, "show", review_id)
    thread_id = re.search(r"^### ([0-9a-f]{8}) · feat.py:7", shown, re.M).group(1)
    merlin_review(tmux_env, "reply", review_id, thread_id, "Noted.")
    page.wait_for_selector("#file-content .thread-reply", timeout=15000)
    assert "Noted." in page.inner_text("#file-content .thread-reply")
    # A fresh page on the file URL follows a resolve too
    page.goto(f"{server}/commits/reviews/{review_id}/file/feat.py")
    page.wait_for_selector("#file-content .thread.open")
    merlin_review(tmux_env, "resolve", review_id, thread_id, "-m", "Done.")
    page.wait_for_selector("#file-content .thread.resolved", timeout=15000)
    assert (
        page.inner_text("#file-content .thread-resolved-label")
        == "Resolved · 2 replies"
    )


def test_line_threads_survive_a_deleted_branch(page, server, repo, tmux_env):
    """With the branch gone the page shows the error, every line thread in
    the review panel with its quote, and still follows the agent's replies."""
    _open_branch_comparison(page, server, repo)
    _comment_on(page, "#diff-file-feat\\.py", "new", 2, "keep me")
    page.wait_for_function("() => location.pathname.startsWith('/commits/reviews/')")
    review_id = _review_id(page)
    page.wait_for_selector("#diff-file-feat\\.py .comment-thread-row .thread")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "-q", "-D", "feature/x")
    page.reload()
    page.wait_for_selector("#review-error", state="visible")
    assert "Unknown ref: feature/x" in page.inner_text("#review-error")
    assert page.query_selector_all(".diff-file-section") == []
    page.wait_for_selector("#review-threads .thread")
    thread = page.query_selector("#review-threads .thread")
    assert "keep me" in thread.inner_text()
    assert thread.query_selector(".thread-where").inner_text() == "feat.py:2 (new)"
    assert "print(2)" in thread.query_selector(".thread-quote").inner_text()
    shown = merlin_review(tmux_env, "show", review_id)
    thread_id = re.search(r"^### ([0-9a-f]{8}) · feat.py:2", shown, re.M).group(1)
    merlin_review(tmux_env, "reply", review_id, thread_id, "Still here.")
    page.wait_for_selector("#review-threads .thread-reply", timeout=15000)
    assert "Still here." in page.inner_text("#review-threads .thread-reply")
    merlin_review(tmux_env, "resolve", review_id, thread_id)
    page.wait_for_selector("#review-threads .thread.resolved", timeout=15000)


def test_phone_comment_controls_are_44px(page, server, repo):
    if page.viewport_size["width"] >= 768:
        pytest.skip("phone only")
    _open_branch_comparison(page, server, repo)
    row = page.query_selector("#diff-file-a\\.txt tr[data-side='new'][data-line='2']")
    row.query_selector_all(".diff-line-no")[-1].click()
    box = row.query_selector(".comment-add-btn").bounding_box()
    assert box["height"] >= 44 and box["width"] >= 44, box
    row.query_selector(".comment-add-btn").click()
    for sel in (".composer-submit", ".composer-cancel"):
        assert (
            page.query_selector(f".comment-composer-row {sel}").bounding_box()["height"]
            >= 44
        )
    page.fill(".comment-composer-row textarea", "tap")
    page.click(".comment-composer-row .composer-submit")
    page.wait_for_selector("#diff-file-a\\.txt .thread")
    for sel in (".thread-reply-btn", ".thread-resolve-btn"):
        assert (
            page.query_selector(f"#diff-file-a\\.txt .thread {sel}").bounding_box()[
                "height"
            ]
            >= 44
        )
