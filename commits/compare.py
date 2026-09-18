"""``base..head`` comparisons for the commit browser.

Every view of the Commits page is a comparison of two points of the
repository: a single commit is ``commit^..commit``, a range picked in the
list is ``oldest^..newest``, a branch against its base is
``merge-base(base, head)..head``, and the working tree is ``HEAD`` against
the files on disk. One family of functions produces the file list with
statuses and stats, the parsed diff and the full file with gutters for any
comparison, and the single-commit routes call them as a special case.

Ref safety: user input reaches git here as something other than a hex hash
for the first time. Every ref is matched by ``REF_RE`` (never starting with
``-``, never containing ``..``), resolved with ``git rev-parse --verify
--end-of-options <ref>^{commit}`` before any other command runs, and every
git invocation built from user input puts ``--end-of-options`` before refs
and ``--`` before paths.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from . import git_parser as gp

# git's empty tree: the base of a root commit, which has no parent.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Any ref git understands (branches with slashes, tags, HEAD~3, abc123^,
# origin/main, main@{upstream}). Never a leading dash, never ``..``.
REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+^~@{}-]*")

# Included commits listed in a comparison detail, at most.
MAX_COMMITS = 200


class RefError(ValueError):
    """A ref that is malformed or does not resolve to a commit (a 400)."""


def validate_ref(ref: str) -> str:
    """Return ``ref`` if it has a safe shape, else raise ``RefError``.

    The shape check is not the safety mechanism, the resolve step and
    ``--end-of-options`` are. It only rejects what can never be a ref.
    """
    # fullmatch, not match with ``$``: ``$`` would accept a trailing newline.
    if not ref or ref.startswith("-") or ".." in ref or not REF_RE.fullmatch(ref):
        raise RefError(f"Invalid ref: {ref!r}")
    return ref


def resolve_ref(ref: str, repo_dir: Path) -> str:
    """Resolve ``ref`` to a full commit sha, or raise ``RefError``."""
    validate_ref(ref)
    try:
        out = gp._run_git(
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{ref}^{{commit}}",
            repo_dir=repo_dir,
        )
    except subprocess.CalledProcessError:
        raise RefError(f"Unknown ref: {ref}")
    sha = out.strip()
    if len(sha) != 40 or not gp.HASH_RE.match(sha):
        raise RefError(f"Unknown ref: {ref}")
    return sha


def _parents(sha: str, repo_dir: Path) -> list[str]:
    """Parent shas of a resolved commit (empty for a root commit)."""
    out = gp._run_git(
        "log", "-1", "--format=%P", "--end-of-options", sha, "--", repo_dir=repo_dir
    )
    return out.split()


def parent_or_empty_tree(sha: str, repo_dir: Path) -> str:
    """First parent of ``sha``, or the empty tree when it is a root commit."""
    parents = _parents(sha, repo_dir)
    return parents[0] if parents else EMPTY_TREE


def _short(sha: str | None) -> str | None:
    return sha[:7] if sha else None


@dataclass
class Comparison:
    """A resolved ``base..head``.

    ``base`` and ``head`` are what the user typed (kept so a branch review
    can follow its branch). ``base_resolved`` is what ``base`` resolved to
    (the empty tree for the parent of a root commit), ``merge_base`` is set
    when ``mergebase`` is on, and ``diff_base`` is the sha every diff runs
    against. ``head_resolved`` is None for the working tree, which has no
    head commit.
    """

    kind: str  # commit | range | branch | worktree
    base: str
    head: str
    mergebase: bool
    worktree: bool
    base_resolved: str
    head_resolved: str | None
    merge_base: str | None
    diff_base: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["base_short"] = _short(self.base_resolved)
        d["head_short"] = _short(self.head_resolved)
        d["merge_base_short"] = _short(self.merge_base)
        d["diff_base_short"] = _short(self.diff_base)
        return d


def _looks_like_hash(ref: str) -> bool:
    """A head typed as a commit hash (from the sheet's recent commits or by
    hand) is not a branch: merge-base mode then yields a range, not a
    branch review that would claim to follow something that cannot move."""
    return bool(re.fullmatch(r"[0-9a-f]{4,40}", ref))


def _resolve_base(base: str, head_resolved: str, repo_dir: Path) -> str:
    """Resolve the base ref, with ``<root>^`` standing for the empty tree.

    The list's Select mode builds ``oldest^`` for the base. When ``oldest``
    is the repository's root commit that ref does not exist, and decision 1
    says the base is then git's empty tree.
    """
    try:
        return resolve_ref(base, repo_dir)
    except RefError:
        if base.endswith("^") and not base.endswith("^^"):
            try:
                commit = resolve_ref(base[:-1], repo_dir)
            except RefError:
                raise RefError(f"Unknown ref: {base}")
            if not _parents(commit, repo_dir):
                return EMPTY_TREE
        raise


def resolve_comparison(
    repo_dir: Path,
    *,
    base: str = "",
    head: str = "",
    mergebase: bool = False,
    worktree: bool = False,
) -> Comparison:
    """Validate and resolve the refs of a comparison. Raises ``RefError``."""
    if worktree:
        base = base or "HEAD"
        base_resolved = resolve_ref(base, repo_dir)
        return Comparison(
            kind="worktree",
            base=base,
            head="",
            mergebase=False,
            worktree=True,
            base_resolved=base_resolved,
            head_resolved=None,
            merge_base=None,
            diff_base=base_resolved,
        )

    if not head:
        raise RefError("A head ref is required")
    if not base:
        raise RefError("A base ref is required")
    head_resolved = resolve_ref(head, repo_dir)
    base_resolved = _resolve_base(base, head_resolved, repo_dir)

    merge_base: str | None = None
    if mergebase:
        if base_resolved == EMPTY_TREE:
            merge_base = EMPTY_TREE
        else:
            try:
                out = gp._run_git(
                    "merge-base",
                    "--end-of-options",
                    base_resolved,
                    head_resolved,
                    repo_dir=repo_dir,
                )
            except subprocess.CalledProcessError:
                raise RefError(f"No merge base between {base} and {head}")
            merge_base = out.strip()
    diff_base = merge_base if merge_base else base_resolved

    if mergebase and not _looks_like_hash(head):
        kind = "branch"
    elif diff_base == parent_or_empty_tree(head_resolved, repo_dir):
        kind = "commit"
    else:
        kind = "range"

    return Comparison(
        kind=kind,
        base=base,
        head=head,
        mergebase=mergebase,
        worktree=False,
        base_resolved=base_resolved,
        head_resolved=head_resolved,
        merge_base=merge_base,
        diff_base=diff_base,
    )


def commit_comparison(commit_hash: str, repo_dir: Path) -> Comparison:
    """The single-commit special case: ``commit^..commit``."""
    h = gp._validate_hash(commit_hash)
    head_resolved = resolve_ref(h, repo_dir)
    base_resolved = parent_or_empty_tree(head_resolved, repo_dir)
    return Comparison(
        kind="commit",
        base=f"{h}^",
        head=h,
        mergebase=False,
        worktree=False,
        base_resolved=base_resolved,
        head_resolved=head_resolved,
        merge_base=None,
        diff_base=base_resolved,
    )


# ---------------------------------------------------------------------------
# Working tree helpers
# ---------------------------------------------------------------------------


def _untracked_files(repo_dir: Path) -> list[str]:
    """Untracked, non-ignored files, as git lists them."""
    out = gp._run_git(
        "ls-files", "--others", "--exclude-standard", "-z", repo_dir=repo_dir
    )
    return [p for p in out.split("\0") if p]


def _is_tracked(path: str, repo_dir: Path) -> bool:
    out = gp._run_git("ls-files", "-z", "--", path, repo_dir=repo_dir, check=False)
    return bool(out.strip("\0"))


def _untracked_diff(path: str, repo_dir: Path) -> str:
    """A unified diff showing an untracked file as added (``--no-index``
    exits 1 when the files differ, which is the expected case)."""
    return gp._run_git(
        "diff", "--no-index", "--", "/dev/null", path, repo_dir=repo_dir, check=False
    )


def _count_lines(path: Path) -> int:
    """Lines of a regular text file on disk, streamed so a large file never
    lands in memory at once. 0 for a binary file (git's heuristic: a NUL
    byte in the first 8000 bytes). The caller has already proved ``path``
    is a regular file inside the repository."""
    lines = 0
    ends_with_newline = True
    try:
        with path.open("rb") as fh:
            first = True
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                if first:
                    first = False
                    if b"\0" in chunk[:8000]:
                        return 0
                lines += chunk.count(b"\n")
                ends_with_newline = chunk.endswith(b"\n")
    except OSError:
        return 0
    if first:
        return 0
    return lines + (0 if ends_with_newline else 1)


def _contained_file(file_path: str, repo_dir: Path) -> Path | None:
    """The regular file ``file_path`` names inside the repository root, with
    symlinks followed, or None when it is missing, not a regular file, or
    resolves outside the root. The one gate every read from disk goes
    through: the full-file view, the untracked line counts, the untracked
    diffs."""
    root = repo_dir.resolve()
    try:
        target = (root / file_path).resolve(strict=True)
    except OSError:
        return None
    if not target.is_relative_to(root):
        return None
    if not target.is_file():
        return None
    return target


def worktree_file_path(file_path: str, repo_dir: Path) -> Path:
    """Resolve a working-tree path and check it stays inside the root.

    Symlinks are followed before the check, so a link that escapes the
    repository is refused (``ValueError``, a 400). A missing path or a
    directory is a ``FileNotFoundError`` (a 404).
    """
    gp._validate_file_path(file_path)
    target = _contained_file(file_path, repo_dir)
    if target is None:
        root = repo_dir.resolve()
        try:
            resolved = (root / file_path).resolve(strict=True)
        except OSError:
            raise FileNotFoundError(f"File {file_path} not found in the working tree")
        if not resolved.is_relative_to(root):
            raise ValueError(f"Path escapes the repository: {file_path}")
        raise FileNotFoundError(f"File {file_path} not found in the working tree")
    return target


def _readable_untracked(paths: list[str], repo_dir: Path) -> dict[str, Path | None]:
    """Each untracked path with the contained regular file it names, or None
    when it must not be read (a symlink escaping the root, a FIFO, a device,
    a dangling link). Such an entry is still listed as added, with no line
    count and no diff: the tree is shown, the file is never opened."""
    return {p: _contained_file(p, repo_dir) for p in paths}


# ---------------------------------------------------------------------------
# Files, diff, full file, commits
# ---------------------------------------------------------------------------


def _parse_file_stats(numstat: str, name_status: str) -> list[dict]:
    """Merge ``--numstat`` and ``--name-status`` output into file records.

    The parsing is the one the single-commit detail always had, so the
    payload of the existing route is unchanged, rename quirks included.
    """
    status_map = {}
    rename_map = {}
    for line in name_status.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts_ns = line.split("\t")
        if len(parts_ns) >= 2:
            status = parts_ns[0][0]  # First char: M, A, D, R
            if status == "R" and len(parts_ns) >= 3:
                # Rename: R100\told\tnew
                rename_map[parts_ns[2]] = parts_ns[1]
                status_map[parts_ns[2]] = "R"
            else:
                status_map[parts_ns[1]] = status

    files = []
    for line in numstat.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts_num = line.split("\t")
        if len(parts_num) >= 3:
            ins = int(parts_num[0]) if parts_num[0] != "-" else 0
            dels = int(parts_num[1]) if parts_num[1] != "-" else 0
            path = parts_num[2]
            file_info = {
                "path": path,
                "status": status_map.get(path, "M"),
                "insertions": ins,
                "deletions": dels,
            }
            if path in rename_map:
                file_info["old_path"] = rename_map[path]
            files.append(file_info)
    return files


def _status_map(name_status: str) -> dict:
    status_map = {}
    for line in name_status.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            status_map[parts[-1]] = parts[0][0]
    return status_map


def _diff_args(cmp: Comparison) -> list[str]:
    """The revision arguments of a ``git diff`` for this comparison."""
    if cmp.worktree:
        return ["--end-of-options", cmp.diff_base]
    return ["--end-of-options", cmp.diff_base, cmp.head_resolved or ""]


def compare_files(cmp: Comparison, repo_dir: Path) -> list[dict]:
    """Files touched by the comparison: path, status, insertions, deletions."""
    revs = _diff_args(cmp)
    numstat = gp._run_git(
        "diff", "--numstat", *revs, "--", repo_dir=repo_dir, check=False
    )
    name_status = gp._run_git(
        "diff", "--name-status", *revs, "--", repo_dir=repo_dir, check=False
    )
    files = _parse_file_stats(numstat, name_status)
    if cmp.worktree:
        readable = _readable_untracked(_untracked_files(repo_dir), repo_dir)
        for path, target in readable.items():
            files.append(
                {
                    "path": path,
                    "status": "A",
                    "insertions": _count_lines(target) if target else 0,
                    "deletions": 0,
                }
            )
    return files


def compare_diff(cmp: Comparison, repo_dir: Path) -> dict:
    """Parsed unified diff of the comparison, ``{"files": [FileDiff]}``."""
    revs = _diff_args(cmp)
    diff_output = gp._run_git("diff", "-p", *revs, "--", repo_dir=repo_dir, check=False)
    name_status = gp._run_git(
        "diff", "--name-status", *revs, "--", repo_dir=repo_dir, check=False
    )
    status_map = _status_map(name_status)
    if cmp.worktree:
        parts = [diff_output]
        readable = _readable_untracked(_untracked_files(repo_dir), repo_dir)
        for path, target in readable.items():
            status_map[path] = "A"
            if target is None:
                # Listed, never read: an empty file diff with no hunks.
                parts.append(f"diff --git a/{path} b/{path}\n")
            else:
                parts.append(_untracked_diff(path, repo_dir))
        diff_output = "\n".join(p for p in parts if p)
    files = gp._parse_unified_diff(diff_output, status_map)
    return {"files": files}


def compare_file(cmp: Comparison, file_path: str, repo_dir: Path) -> dict:
    """Full file at the head of the comparison, with gutter annotations."""
    gp._validate_file_path(file_path)

    if cmp.worktree:
        target = worktree_file_path(file_path, repo_dir)
        content = target.read_text(errors="replace")
        if _is_tracked(file_path, repo_dir):
            diff_output = gp._run_git(
                "diff",
                "--end-of-options",
                cmp.diff_base,
                "--",
                file_path,
                repo_dir=repo_dir,
                check=False,
            )
        else:
            diff_output = _untracked_diff(file_path, repo_dir)
    else:
        content = _blob_at(cmp.head_resolved or "", cmp.head, file_path, repo_dir)
        diff_output = gp._run_git(
            "diff",
            "--end-of-options",
            cmp.diff_base,
            cmp.head_resolved or "",
            "--",
            file_path,
            repo_dir=repo_dir,
            check=False,
        )

    return {
        "content": content,
        "lines": gp._compute_gutters(diff_output, content),
    }


def _blob_at(head_sha: str, head: str, file_path: str, repo_dir: Path) -> str:
    """The content of ``file_path`` at ``head_sha``.

    The path is user input, so it is never embedded in a revision argument
    like ``<sha>:<path>``. ``git ls-tree --end-of-options <sha> -- <path>``
    names the blob, and ``git cat-file blob <oid>`` reads it by its validated
    object id. A missing path, or one that is a directory, is a
    ``FileNotFoundError``.
    """
    entry = gp._run_git(
        "ls-tree",
        "-z",
        "--end-of-options",
        head_sha,
        "--",
        file_path,
        repo_dir=repo_dir,
        check=False,
    )
    line = entry.split("\0", 1)[0]
    # "<mode> <type> <oid>\t<path>"
    meta, _, _ = line.partition("\t")
    fields = meta.split()
    if len(fields) != 3 or fields[1] != "blob" or not gp.HASH_RE.match(fields[2]):
        raise FileNotFoundError(f"File {file_path} not found at {head}")
    oid = fields[2]
    if len(oid) != 40:
        raise FileNotFoundError(f"File {file_path} not found at {head}")
    try:
        return gp._run_git(
            "cat-file", "blob", "--end-of-options", oid, repo_dir=repo_dir
        )
    except subprocess.CalledProcessError:
        raise FileNotFoundError(f"File {file_path} not found at {head}")


def compare_patch(cmp: Comparison, repo_dir: Path, path: str | None = None) -> str:
    """The unified diff of the comparison as git prints it, for the agent's
    ``merlin review diff``. Optionally one file. Untracked working-tree
    files are appended through the same containment gate as the page."""
    revs = _diff_args(cmp)
    paths = ["--", path] if path else ["--"]
    if path:
        gp._validate_file_path(path)
    out = gp._run_git("diff", *revs, *paths, repo_dir=repo_dir, check=False)
    if cmp.worktree:
        readable = _readable_untracked(_untracked_files(repo_dir), repo_dir)
        parts = [out] if out else []
        for p, target in readable.items():
            if path and p != path:
                continue
            if target is not None:
                parts.append(_untracked_diff(p, repo_dir))
        out = "".join(part if part.endswith("\n") else part + "\n" for part in parts)
    return out


def side_lines(
    cmp: Comparison, path: str, side: str, repo_dir: Path
) -> list[str] | None:
    """The lines of ``path`` on one side of the comparison: ``new`` is the
    head version (the disk for the working tree), ``old`` is the base
    version. None when the file does not exist on that side."""
    gp._validate_file_path(path)
    try:
        if side == "new":
            if cmp.worktree:
                content = worktree_file_path(path, repo_dir).read_text(errors="replace")
            else:
                content = _blob_at(cmp.head_resolved or "", cmp.head, path, repo_dir)
        elif side == "old":
            content = _blob_at(cmp.diff_base, cmp.base, path, repo_dir)
        else:
            raise ValueError(f"Unknown side: {side}")
    except (FileNotFoundError, ValueError):
        return None
    if cmp.diff_base == EMPTY_TREE and side == "old":
        return None
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _parse_commit_lines(output: str) -> list[dict]:
    commits = []
    for line in output.strip().split("\n"):
        parts = line.split("|", 4)
        if len(parts) == 5 and gp.HASH_RE.match(parts[0]) and len(parts[0]) == 40:
            commits.append(
                {
                    "hash": parts[0],
                    "short": parts[1],
                    "author": parts[2],
                    "date": parts[3],
                    "message": parts[4],
                }
            )
    return commits


def compare_commits(
    cmp: Comparison, repo_dir: Path
) -> tuple[list[dict], int, dict | None]:
    """Commits included in ``diff_base..head`` (newest first, at most
    ``MAX_COMMITS``), the total count, and the oldest included commit.

    The oldest is carried separately because the list is capped: a title
    that takes the last listed commit as the range's start would be wrong
    past 200 commits. Empty for the working tree.
    """
    if cmp.worktree or not cmp.head_resolved:
        return [], 0, None
    if cmp.diff_base == EMPTY_TREE:
        rev = cmp.head_resolved
    else:
        rev = f"{cmp.diff_base}..{cmp.head_resolved}"
    shas = gp._run_git(
        "rev-list", "--end-of-options", rev, "--", repo_dir=repo_dir
    ).split()
    count = len(shas)
    if count == 0:
        return [], 0, None
    log_out = gp._run_git(
        "log",
        "--format=%H|%h|%an|%aI|%s",
        f"--max-count={MAX_COMMITS}",
        "--end-of-options",
        rev,
        "--",
        repo_dir=repo_dir,
    )
    commits = _parse_commit_lines(log_out)
    if count <= len(commits):
        oldest = commits[-1] if commits else None
    else:
        oldest_out = gp._run_git(
            "log",
            "-1",
            "--format=%H|%h|%an|%aI|%s",
            "--end-of-options",
            shas[-1],
            "--",
            repo_dir=repo_dir,
        )
        parsed = _parse_commit_lines(oldest_out)
        oldest = parsed[0] if parsed else None
    return commits, count, oldest


def compare_detail(cmp: Comparison, repo_dir: Path) -> dict:
    """The payload of ``GET /api/commits/compare``."""
    commits, count, oldest = compare_commits(cmp, repo_dir)
    detail = cmp.to_dict()
    detail["commits"] = commits
    detail["commit_count"] = count
    detail["oldest"] = oldest
    detail["files"] = compare_files(cmp, repo_dir)
    return detail


# ---------------------------------------------------------------------------
# Refs (the compare sheet's branch lists)
# ---------------------------------------------------------------------------


def _current_branch(repo_dir: Path) -> str | None:
    out = gp._run_git(
        "symbolic-ref", "--short", "-q", "HEAD", repo_dir=repo_dir, check=False
    )
    return out.strip() or None


def _branches(repo_dir: Path, namespace: str) -> list[str]:
    out = gp._run_git(
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname)",
        namespace,
        repo_dir=repo_dir,
        check=False,
    )
    prefix = namespace.rstrip("/") + "/"
    names = []
    for line in out.split("\n"):
        line = line.strip()
        if not line.startswith(prefix):
            continue
        name = line[len(prefix) :]
        if namespace == "refs/remotes" and name.endswith("/HEAD"):
            continue
        names.append(name)
    return names


def guess_default_base(
    local: list[str], remote: list[str], current: str | None, origin_head: str | None
) -> str | None:
    """The base the compare sheet proposes.

    The target of ``refs/remotes/origin/HEAD`` if it exists (as the local
    branch of that name when there is one), else ``main``, else ``master``,
    else the first other local branch. A candidate equal to the current
    branch is skipped: comparing a branch to itself is empty.
    """
    candidates: list[str] = []
    if origin_head:
        # origin_head is "origin/main"
        _, _, name = origin_head.partition("/")
        if name and name in local:
            candidates.append(name)
        candidates.append(origin_head)
    candidates += ["main", "master"]
    candidates += local
    for c in candidates:
        if c == current:
            continue
        if c in local or c in remote:
            return c
    return None


RECENT_COMMITS = 30


def _recent_commits(repo_dir: Path) -> list[dict]:
    """The last commits reachable from HEAD, newest first, for the sheet's
    commit pickers. Empty in a repository with no commits."""
    out = gp._run_git(
        "log",
        "--format=%H|%h|%an|%aI|%s",
        f"--max-count={RECENT_COMMITS}",
        "HEAD",
        "--",
        repo_dir=repo_dir,
        check=False,
    )
    return _parse_commit_lines(out)


def list_refs(repo_dir: Path) -> dict:
    """Local and remote branches, the current branch, the guessed base, the
    recent commits."""
    current = _current_branch(repo_dir)
    local = _branches(repo_dir, "refs/heads")
    remote = _branches(repo_dir, "refs/remotes")
    origin_head = gp._run_git(
        "symbolic-ref",
        "-q",
        "--short",
        "refs/remotes/origin/HEAD",
        repo_dir=repo_dir,
        check=False,
    ).strip()
    return {
        "current": current,
        "local": local,
        "remote": remote,
        "default_base": guess_default_base(local, remote, current, origin_head or None),
        "commits": _recent_commits(repo_dir),
    }


# ---------------------------------------------------------------------------
# The single-commit routes, as the special case
# ---------------------------------------------------------------------------


def get_commit_detail(commit_hash: str, repo_dir: Path) -> dict:
    """Single commit metadata with file stats. Same payload as before the
    comparison model: hash, short, message, body, author, date, files."""
    h = gp._validate_hash(commit_hash)

    meta_output = gp._run_git(
        "show",
        "--format=%H|%h|%an|%aI|%s|%b",
        "--no-patch",
        "--end-of-options",
        h,
        "--",
        repo_dir=repo_dir,
    )
    meta_line = meta_output.strip().split("\n")[0] if meta_output.strip() else ""
    parts = meta_line.split("|", 5)
    if len(parts) < 5:
        raise ValueError(f"Could not parse commit {h}")

    cmp = commit_comparison(h, repo_dir)
    return {
        "hash": parts[0],
        "short": parts[1],
        "author": parts[2],
        "date": parts[3],
        "message": parts[4],
        "body": parts[5].strip() if len(parts) > 5 else "",
        "files": compare_files(cmp, repo_dir),
    }


def get_commit_diff(commit_hash: str, repo_dir: Path) -> dict:
    """Parsed unified diff for a commit (``commit^..commit``)."""
    cmp = commit_comparison(commit_hash, repo_dir)
    return compare_diff(cmp, repo_dir)


def get_file_with_gutters(commit_hash: str, file_path: str, repo_dir: Path) -> dict:
    """Full file content at a commit with gutter annotations."""
    gp._validate_file_path(file_path)
    cmp = commit_comparison(commit_hash, repo_dir)
    return compare_file(cmp, file_path, repo_dir)
