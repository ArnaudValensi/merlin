"""Saved reviews: a comparison kept under ``~/.merlin/reviews/``.

One JSON file per review, ``<id>.json``, written atomically (temp file then
rename) under an ``fcntl.flock`` on ``<id>.lock``, because the server and
the ``merlin review`` CLI write the same file from different processes.
Every read-modify-write goes through ``update()`` and runs inside the lock.
A malformed file is reported (``ReviewCorrupt``), never replaced.

The record shape is decision 7 of the commit-review epic: the refs as the
user typed them (so a branch review follows its branch), ``base_resolved``
recorded at creation, ``last_seen_head`` for the "N new commits since you
last looked" line, ``files`` with a ``viewed_hash`` per viewed file, and
``comments`` (threads, milestone 3).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import paths

from . import compare as cm
from . import git_parser as gp

ID_RE = re.compile(r"[0-9a-f]{8}")
STATUSES = ("open", "closed")
TITLE_MAX = 200
AUTHORS = ("user", "agent")
SIDES = ("new", "old")
BODY_MAX = 4000
ANCHOR_STATES = ("current", "moved", "outdated")


class ReviewNotFound(KeyError):
    """No review file for that id (a 404)."""


class ReviewCorrupt(ValueError):
    """The review file is not the JSON object it should be. Reported, never
    replaced: the user's data is in that file."""


def reviews_dir() -> Path:
    return paths.reviews_dir()


def new_id() -> str:
    return secrets.token_hex(4)


def validate_id(review_id: str) -> str:
    if not review_id or not ID_RE.fullmatch(review_id):
        raise ValueError(f"Invalid review id: {review_id!r}")
    return review_id


def now_iso() -> str:
    """Microsecond precision: two writes in the same second (a tick, then
    the agent's reply) must still give the poll two different stamps."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="microseconds")


def _path(review_id: str) -> Path:
    return reviews_dir() / f"{validate_id(review_id)}.json"


def _lock_path(review_id: str) -> Path:
    return reviews_dir() / f"{validate_id(review_id)}.lock"


@contextmanager
def locked(review_id: str) -> Iterator[None]:
    """Hold the review's lock for the block. Blocking, exclusive, released
    when the file handle closes even if the process dies."""
    d = reviews_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(_lock_path(review_id), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(review_id: str) -> dict:
    path = _path(review_id)
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ReviewNotFound(review_id)
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ReviewCorrupt(f"Review file is malformed: {path} ({e})")
    if not isinstance(data, dict) or data.get("id") != review_id:
        raise ReviewCorrupt(f"Review file is malformed: {path}")
    return data


def _write(review: dict) -> None:
    """Atomic: the whole record lands in a temp file, then one rename."""
    path = _path(review["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as fh:
        json.dump(review, fh, indent=2, sort_keys=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load(review_id: str) -> dict:
    """The record as stored. Reads need no lock: a rename is atomic, so a
    reader sees the previous or the next whole file, never a partial one."""
    return _read(review_id)


def update(review_id: str, fn: Callable[[dict], None]) -> dict:
    """Read, apply ``fn`` and write back, all inside the lock. ``updated``
    is stamped on every write."""
    with locked(review_id):
        review = _read(review_id)
        fn(review)
        review["updated"] = now_iso()
        _write(review)
    return review


def list_for_repo(repo: str) -> list[dict]:
    """Every review of that repository root, newest update first. A file
    that cannot be parsed is listed as an error entry, not skipped."""
    d = reviews_dir()
    if not d.is_dir():
        return []
    out = []
    for path in sorted(d.glob("*.json")):
        review_id = path.stem
        if not ID_RE.fullmatch(review_id):
            continue
        try:
            review = _read(review_id)
        except ReviewCorrupt as e:
            out.append({"id": review_id, "error": str(e), "repo": None})
            continue
        if review.get("repo") != repo:
            continue
        out.append(summary(review))
    out.sort(key=lambda r: r.get("updated") or "", reverse=True)
    return out


def summary(review: dict) -> dict:
    comments = review.get("comments") or []
    return {
        "id": review["id"],
        "title": review.get("title", ""),
        "kind": review.get("kind"),
        "status": review.get("status", "open"),
        "base": review.get("base"),
        "head": review.get("head"),
        "mergebase": bool(review.get("mergebase")),
        "created": review.get("created"),
        "updated": review.get("updated"),
        "viewed_count": len(review.get("files") or {}),
        "open_comments": sum(1 for c in comments if c.get("status") == "open"),
    }


# ---------------------------------------------------------------------------
# From a review to its comparison, and back
# ---------------------------------------------------------------------------


def comparison_of(review: dict, repo_dir: Path) -> cm.Comparison:
    """Resolve the review's comparison now. The merge base is recomputed on
    every load, so a base branch that advances is followed."""
    return cm.resolve_comparison(
        repo_dir,
        base=review.get("base") or "",
        head=review.get("head") or "",
        mergebase=bool(review.get("mergebase")),
        worktree=review.get("kind") == "worktree",
    )


def default_title(cmp: cm.Comparison, repo_dir: Path) -> str:
    """The head branch and base for a branch, the subject for a single commit,
    ``<oldest>..<newest> (N commits)`` for a range, ``Working tree``."""
    if cmp.worktree:
        return "Working tree"
    if cmp.kind == "branch":
        return f"{cmp.head} vs {cmp.base}"
    commits, count, oldest = cm.compare_commits(cmp, repo_dir)
    if cmp.kind == "commit" and commits:
        return commits[0]["message"]
    if commits and oldest:
        return (
            f"{oldest['short']}..{commits[0]['short']} ({cm_plural(count, 'commit')})"
        )
    return f"{cmp.base}..{cmp.head}"


def cm_plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def create(repo_dir: Path, cmp: cm.Comparison, title: str | None = None) -> dict:
    """A new review of ``cmp``, written under its lock. Returns the record."""
    review_id = new_id()
    while _path(review_id).exists():
        review_id = new_id()
    stamp = now_iso()
    review = {
        "id": review_id,
        "repo": str(repo_dir.resolve()),
        "title": clean_title(title) or default_title(cmp, repo_dir),
        "kind": cmp.kind,
        "base": cmp.base,
        "head": cmp.head,
        "mergebase": cmp.mergebase,
        "base_resolved": cmp.base_resolved,
        "head_resolved": cmp.head_resolved,
        "last_seen_head": cmp.head_resolved,
        "last_seen_at": stamp,
        "status": "open",
        "created": stamp,
        "updated": stamp,
        "files": {},
        "comments": [],
    }
    with locked(review_id):
        _write(review)
    return review


def clean_title(title: str | None) -> str:
    if not title:
        return ""
    return " ".join(title.split())[:TITLE_MAX]


def set_title(review_id: str, title: str) -> dict:
    clean = clean_title(title)
    if not clean:
        raise ValueError("A title cannot be empty")

    def fn(review: dict) -> None:
        review["title"] = clean

    return update(review_id, fn)


def set_status(review_id: str, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"Status must be one of {', '.join(STATUSES)}")

    def fn(review: dict) -> None:
        review["status"] = status

    return update(review_id, fn)


# ---------------------------------------------------------------------------
# Viewed files: the hash of the file's patch text (decision 10)
# ---------------------------------------------------------------------------


def patch_text(cmp: cm.Comparison, path: str, repo_dir: Path) -> str:
    """The file's patch in the current comparison, as git prints it."""
    gp._validate_file_path(path)
    if cmp.worktree:
        if cm._is_tracked(path, repo_dir):
            return gp._run_git(
                "diff",
                "--end-of-options",
                cmp.diff_base,
                "--",
                path,
                repo_dir=repo_dir,
                check=False,
            )
        if cm._contained_file(path, repo_dir) is None:
            return ""
        return cm._untracked_diff(path, repo_dir)
    return gp._run_git(
        "diff",
        "--end-of-options",
        cmp.diff_base,
        cmp.head_resolved or "",
        "--",
        path,
        repo_dir=repo_dir,
        check=False,
    )


def patch_hash(cmp: cm.Comparison, path: str, repo_dir: Path) -> str:
    return hashlib.sha1(patch_text(cmp, path, repo_dir).encode()).hexdigest()


def set_viewed(review_id: str, path: str, viewed: bool, repo_dir: Path) -> dict:
    """Tick or untick a file. A tick stores the hash of the file's patch in
    the comparison as it is now, so a later change to that patch clears it."""
    gp._validate_file_path(path)

    def fn(review: dict) -> None:
        files = review.setdefault("files", {})
        if not viewed:
            files.pop(path, None)
            return
        cmp = comparison_of(review, repo_dir)
        files[path] = {
            "viewed_at": now_iso(),
            "viewed_hash": patch_hash(cmp, path, repo_dir),
        }

    return update(review_id, fn)


# ---------------------------------------------------------------------------
# Comments: threads anchored to a line, or to the review (decision 11)
# ---------------------------------------------------------------------------


def clean_body(body: str | None) -> str:
    """Plain text, line breaks kept, at most BODY_MAX characters, never
    empty. No markdown is interpreted anywhere."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("A comment cannot be empty")
    if len(text) > BODY_MAX:
        raise ValueError(f"A comment is at most {BODY_MAX} characters")
    return text


def _author(author: str | None) -> str:
    a = (author or "user").strip().lower()
    if a not in AUTHORS:
        raise ValueError(f"Author must be one of {', '.join(AUTHORS)}")
    return a


def _find_comment(review: dict, comment_id: str) -> dict:
    for c in review.get("comments") or []:
        if c.get("id") == comment_id:
            return c
    raise KeyError(f"Unknown comment: {comment_id}")


def add_comment(
    review_id: str,
    repo_dir: Path,
    *,
    body: str,
    author: str = "user",
    path: str | None = None,
    side: str = "new",
    line: int | None = None,
) -> tuple[dict, dict]:
    """A new thread. With ``path`` and ``line`` it anchors to that line on
    ``side`` of the current comparison and records the line's text, and a
    line that does not exist there is an error. Without a path it is a
    review-wide comment. Returns (review, comment)."""
    text = clean_body(body)
    who = _author(author)
    if path is not None:
        gp._validate_file_path(path)
        if side not in SIDES:
            raise ValueError(f"Side must be one of {', '.join(SIDES)}")
        if line is None or line < 1:
            raise ValueError("A line comment needs a line number of 1 or more")
    elif line is not None:
        raise ValueError("A line number needs a path")
    created: dict = {}

    def fn(review: dict) -> None:
        comment = {
            "id": new_id(),
            "path": None,
            "side": None,
            "line": None,
            "line_text": None,
            "anchor_head": None,
            "author": who,
            "body": text,
            "created": now_iso(),
            "status": "open",
            "resolved_at": None,
            "resolved_by": None,
            "replies": [],
        }
        if path is not None:
            cmp = comparison_of(review, repo_dir)
            lines = cm.side_lines(cmp, path, side, repo_dir)
            if lines is None:
                raise ValueError(f"{path} has no {side} side in this comparison")
            assert line is not None
            if line > len(lines):
                raise ValueError(f"{path} has {len(lines)} lines on the {side} side")
            comment.update(
                {
                    "path": path,
                    "side": side,
                    "line": line,
                    "line_text": lines[line - 1],
                    "anchor_head": cmp.head_resolved,
                }
            )
        review.setdefault("comments", []).append(comment)
        created.update(comment)

    review = update(review_id, fn)
    return review, created


def reply(review_id: str, comment_id: str, body: str, author: str = "user") -> dict:
    """A reply on a thread, open or resolved. Returns the review."""
    text = clean_body(body)
    who = _author(author)

    def fn(review: dict) -> None:
        comment = _find_comment(review, comment_id)
        comment.setdefault("replies", []).append(
            {"id": new_id(), "author": who, "body": text, "created": now_iso()}
        )

    return update(review_id, fn)


def resolve(
    review_id: str, comment_id: str, author: str = "user", message: str | None = None
) -> dict:
    """Resolve a thread, with an optional closing reply. Returns the review."""
    who = _author(author)
    text = clean_body(message) if message else None

    def fn(review: dict) -> None:
        comment = _find_comment(review, comment_id)
        if text:
            comment.setdefault("replies", []).append(
                {"id": new_id(), "author": who, "body": text, "created": now_iso()}
            )
        comment["status"] = "resolved"
        comment["resolved_at"] = now_iso()
        comment["resolved_by"] = who

    return update(review_id, fn)


def reopen(review_id: str, comment_id: str) -> dict:
    """Reopen a resolved thread. Returns the review."""

    def fn(review: dict) -> None:
        comment = _find_comment(review, comment_id)
        comment["status"] = "open"
        comment["resolved_at"] = None
        comment["resolved_by"] = None

    return update(review_id, fn)


def anchor_comment(
    comment: dict, lines: list[str] | None, head: str | None
) -> tuple[str, int | None]:
    """The re-anchoring rule (decision 12), one comment against the current
    lines of its side. Returns (state, line): ``current`` when the recorded
    text is still at the recorded line, ``moved`` with the new line when the
    text occurs exactly once elsewhere, ``outdated`` otherwise. Exact text
    on both sides, nothing trimmed. ``lines`` None means the file has no
    such side any more."""
    text = comment.get("line_text")
    line = comment.get("line")
    if lines is None or text is None or not line:
        return "outdated", None
    if 1 <= line <= len(lines) and lines[line - 1] == text:
        return "current", line
    hits = [i + 1 for i, current in enumerate(lines) if current == text]
    if len(hits) == 1:
        return "moved", hits[0]
    return "outdated", None


def anchor_comments(
    comments: list[dict],
    side_lines_of: Callable[[str, str], list[str] | None],
    head: str | None,
) -> dict[str, str]:
    """Re-anchor every line comment whose ``anchor_head`` differs from the
    current head (always for the working tree, whose head is None). A moved
    comment gets its ``line`` and ``anchor_head`` updated in place. Returns
    the state of every line comment by id. Nothing is ever dropped."""
    states: dict[str, str] = {}
    cache: dict[tuple[str, str], list[str] | None] = {}
    for comment in comments:
        path = comment.get("path")
        if not path:
            continue
        if head is not None and comment.get("anchor_head") == head:
            states[comment["id"]] = "current"
            continue
        key = (path, comment.get("side") or "new")
        if key not in cache:
            cache[key] = side_lines_of(*key)
        state, line = anchor_comment(comment, cache[key], head)
        if state == "moved":
            comment["line"] = line
            comment["anchor_head"] = head
        elif state == "current":
            comment["anchor_head"] = head
        states[comment["id"]] = state
    return states


def open_thread_counts(review: dict) -> dict[str, int]:
    """Open threads per path (review-wide ones under the key "")."""
    counts: dict[str, int] = {}
    for c in review.get("comments") or []:
        if c.get("status") != "open":
            continue
        key = c.get("path") or ""
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Loading a review: recompute the viewed state, count the new commits
# ---------------------------------------------------------------------------


def _new_commits_since(last_seen: str, head: str, repo_dir: Path) -> int:
    """Commits in ``last_seen..head``. When the old head is gone (a rebase
    followed by a prune), everything up to the head counts as new."""
    try:
        out = gp._run_git(
            "rev-list",
            "--count",
            "--end-of-options",
            f"{last_seen}..{head}",
            "--",
            repo_dir=repo_dir,
        )
    except subprocess.CalledProcessError:
        out = gp._run_git(
            "rev-list", "--count", "--end-of-options", head, "--", repo_dir=repo_dir
        )
    return int(out.strip() or "0")


class Loaded:
    """What a full load returns: the record, the cleared viewed paths, the
    new-commit count, the live comparison, the anchor state per comment,
    and ``seen_before``, the user's previous visit stamp (what "since your
    last visit" is measured from). Unpacks as the first four for the
    callers that need only those."""

    def __init__(
        self,
        review: dict,
        changed: list[str],
        new_commits: int,
        cmp: cm.Comparison,
        anchors: dict[str, str],
        seen_before: str | None = None,
    ) -> None:
        self.review = review
        self.changed = changed
        self.new_commits = new_commits
        self.cmp = cmp
        self.anchors = anchors
        self.seen_before = seen_before

    def __iter__(self):
        return iter((self.review, self.changed, self.new_commits, self.cmp))

    def __getitem__(self, i: int):
        return (self.review, self.changed, self.new_commits, self.cmp)[i]


def refresh(review_id: str, repo_dir: Path, *, advance_seen: bool = True) -> Loaded:
    """A full load. Inside the lock: resolve the comparison as it is now,
    count the commits since ``last_seen_head`` and only then advance it
    (unless ``advance_seen`` is off: the agent's ``merlin review show`` is
    not the user's look), record the visit in ``last_seen_at`` the same way
    and hand back the previous stamp as ``seen_before``, recompute every
    viewed file's hash and clear the entries whose patch changed, re-anchor
    every line comment. The record is written only when something changed.
    """
    with locked(review_id):
        review = _read(review_id)
        cmp = comparison_of(review, repo_dir)
        changed: list[str] = []
        new_commits = 0
        dirty = False

        last_seen = review.get("last_seen_head")
        head = cmp.head_resolved
        if head and last_seen and head != last_seen:
            new_commits = _new_commits_since(last_seen, head, repo_dir)
        if head != last_seen and advance_seen:
            review["last_seen_head"] = head
            dirty = True

        # The previous visit, for the "since your last visit" panel. A
        # record from before this field existed counts from its creation.
        seen_before = review.get("last_seen_at") or review.get("created")
        if advance_seen:
            review["last_seen_at"] = now_iso()
            dirty = True

        files = review.get("files") or {}
        for path in list(files):
            entry = files[path]
            current = patch_hash(cmp, path, repo_dir)
            if current != (entry or {}).get("viewed_hash"):
                del files[path]
                changed.append(path)
                dirty = True

        comments = review.get("comments") or []
        before = json.dumps(comments, sort_keys=True)
        anchors = anchor_comments(
            comments, lambda p, s: cm.side_lines(cmp, p, s, repo_dir), head
        )
        if json.dumps(comments, sort_keys=True) != before:
            dirty = True

        if dirty:
            review["updated"] = now_iso()
            _write(review)
    return Loaded(review, changed, new_commits, cmp, anchors, seen_before)
