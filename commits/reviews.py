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


def refresh(
    review_id: str, repo_dir: Path
) -> tuple[dict, list[str], int, cm.Comparison]:
    """A full load. Inside the lock: resolve the comparison as it is now,
    count the commits since ``last_seen_head`` and only then advance it,
    recompute every viewed file's hash and clear the entries whose patch
    changed. Returns the record, the cleared paths, the new-commit count and
    the live comparison. The record is written only when something changed.
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
        if head != last_seen:
            review["last_seen_head"] = head
            dirty = True

        files = review.get("files") or {}
        for path in list(files):
            entry = files[path]
            current = patch_hash(cmp, path, repo_dir)
            if current != (entry or {}).get("viewed_hash"):
                del files[path]
                changed.append(path)
                dirty = True

        if dirty:
            review["updated"] = now_iso()
            _write(review)
    return review, changed, new_commits, cmp
