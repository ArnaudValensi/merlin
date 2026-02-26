"""Git command wrappers and output parsing for the commit browser."""

import re
import subprocess
from pathlib import Path

def _find_repo_root(search_dir: str | None = None) -> Path:
    """Find the git repository root directory."""
    cwd = search_dir or str(Path(__file__).parent)
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    # Fallback to project root
    return Path(__file__).parent.parent.resolve()

REPO_DIR = _find_repo_root()


def set_repo_dir(path: str) -> None:
    """Set the git repository directory (called by main.py with CWD)."""
    global REPO_DIR
    REPO_DIR = _find_repo_root(path)

# Validate commit hashes — only allow hex strings (short or long)
HASH_RE = re.compile(r"^[0-9a-f]{4,40}$")


def _validate_hash(commit_hash: str) -> str:
    """Validate a commit hash to prevent command injection."""
    if not HASH_RE.match(commit_hash):
        raise ValueError(f"Invalid commit hash: {commit_hash!r}")
    return commit_hash


def _run_git(*args: str, check: bool = True) -> str:
    """Run a git command in the repo directory and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(REPO_DIR),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ["git", *args], result.stdout, result.stderr
        )
    return result.stdout


def get_commits(
    skip: int = 0,
    limit: int = 50,
    search: str = "",
    since: str = "",
    until: str = "",
) -> list[dict]:
    """Get paginated commit list with stats.

    Returns list of dicts with: hash, short, message, author, date,
    files_changed, insertions, deletions.
    """
    args = [
        "log",
        f"--format=%H|%h|%an|%aI|%s",
        "--shortstat",
        f"--skip={skip}",
        f"--max-count={limit}",
    ]

    if search:
        args.append(f"--grep={search}")
        args.append("--regexp-ignore-case")

    if since:
        args.append(f"--since={since}")

    if until:
        args.append(f"--until={until}")

    output = _run_git(*args, check=False)
    return _parse_log_output(output)


def _parse_log_output(output: str) -> list[dict]:
    """Parse git log output with --shortstat into structured dicts."""
    commits = []
    lines = output.strip().split("\n") if output.strip() else []

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Try to parse as a commit line (pipe-delimited)
        parts = line.split("|", 4)
        if len(parts) == 5 and len(parts[0]) == 40 and HASH_RE.match(parts[0]):
            commit = {
                "hash": parts[0],
                "short": parts[1],
                "author": parts[2],
                "date": parts[3],
                "message": parts[4],
                "files_changed": 0,
                "insertions": 0,
                "deletions": 0,
            }

            # Check next non-empty line for shortstat
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1

            if j < len(lines):
                stat_line = lines[j].strip()
                stats = _parse_shortstat(stat_line)
                if stats:
                    commit.update(stats)
                    i = j + 1
                else:
                    i += 1
            else:
                i += 1

            commits.append(commit)
        else:
            i += 1

    return commits


def _parse_shortstat(line: str) -> dict | None:
    """Parse a --shortstat line like '3 files changed, 10 insertions(+), 2 deletions(-)'.

    Returns dict with files_changed, insertions, deletions or None if not a stat line.
    """
    if "file" not in line and "changed" not in line:
        return None

    stats = {"files_changed": 0, "insertions": 0, "deletions": 0}

    m = re.search(r"(\d+) file", line)
    if m:
        stats["files_changed"] = int(m.group(1))

    m = re.search(r"(\d+) insertion", line)
    if m:
        stats["insertions"] = int(m.group(1))

    m = re.search(r"(\d+) deletion", line)
    if m:
        stats["deletions"] = int(m.group(1))

    return stats


def get_commit_detail(commit_hash: str) -> dict:
    """Get detailed info for a single commit.

    Returns dict with: hash, short, message, body, author, date,
    files: [{path, status, insertions, deletions}].
    """
    h = _validate_hash(commit_hash)

    # Get commit metadata
    meta_output = _run_git(
        "show", "--format=%H|%h|%an|%aI|%s|%b", "--no-patch", h
    )
    meta_line = meta_output.strip().split("\n")[0] if meta_output.strip() else ""
    parts = meta_line.split("|", 5)

    if len(parts) < 5:
        raise ValueError(f"Could not parse commit {h}")

    commit = {
        "hash": parts[0],
        "short": parts[1],
        "author": parts[2],
        "date": parts[3],
        "message": parts[4],
        "body": parts[5].strip() if len(parts) > 5 else "",
        "files": [],
    }

    # Get file stats with --numstat and --name-status
    numstat = _run_git("show", "--format=", "--numstat", h, check=False)
    name_status = _run_git("show", "--format=", "--name-status", h, check=False)

    # Parse name-status for file status (M/A/D/R)
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

    # Parse numstat for insertions/deletions per file
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
            commit["files"].append(file_info)

    return commit


def get_commit_diff(commit_hash: str) -> dict:
    """Get parsed unified diff for a commit.

    Returns dict with files: [{path, status, hunks: [{header, lines: [{type, content, old_no, new_no}]}]}].
    """
    h = _validate_hash(commit_hash)

    diff_output = _run_git("show", "-p", "--format=", h, check=False)

    # Also get name-status for file statuses
    name_status = _run_git("show", "--format=", "--name-status", h, check=False)
    status_map = {}
    for line in name_status.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            status_map[parts[-1]] = parts[0][0]

    files = _parse_unified_diff(diff_output, status_map)
    return {"files": files}


def _parse_unified_diff(diff_text: str, status_map: dict | None = None) -> list[dict]:
    """Parse unified diff output into structured data.

    Returns list of file diffs, each with path, status, and hunks.
    Each hunk has a header and lines with type/content/line numbers.
    """
    if status_map is None:
        status_map = {}

    files = []
    current_file = None
    current_hunk = None

    for line in diff_text.split("\n"):
        # New file diff header
        if line.startswith("diff --git"):
            if current_file:
                if current_hunk:
                    current_file["hunks"].append(current_hunk)
                files.append(current_file)

            # Extract path from "diff --git a/path b/path"
            m = re.match(r"diff --git a/(.*?) b/(.*)", line)
            path = m.group(2) if m else ""
            current_file = {
                "path": path,
                "status": status_map.get(path, "M"),
                "hunks": [],
            }
            current_hunk = None
            continue

        if current_file is None:
            continue

        # Skip diff metadata lines
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("index ") or line.startswith("new file") or line.startswith("deleted file"):
            continue
        if line.startswith("old mode") or line.startswith("new mode"):
            continue
        if line.startswith("similarity index") or line.startswith("rename from") or line.startswith("rename to"):
            continue
        if line.startswith("Binary files"):
            current_file["binary"] = True
            continue

        # Hunk header
        hunk_match = re.match(r"^@@\s+\-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@(.*)", line)
        if hunk_match:
            if current_hunk:
                current_file["hunks"].append(current_hunk)
            current_hunk = {
                "header": line,
                "lines": [],
                "old_start": int(hunk_match.group(1)),
                "new_start": int(hunk_match.group(2)),
            }
            old_no = int(hunk_match.group(1))
            new_no = int(hunk_match.group(2))
            continue

        if current_hunk is None:
            continue

        # Diff content lines
        if line.startswith("+"):
            current_hunk["lines"].append({
                "type": "add",
                "content": line[1:],
                "old_no": None,
                "new_no": new_no,
            })
            new_no += 1
        elif line.startswith("-"):
            current_hunk["lines"].append({
                "type": "del",
                "content": line[1:],
                "old_no": old_no,
                "new_no": None,
            })
            old_no += 1
        elif line.startswith("\\"):
            # "\ No newline at end of file"
            continue
        elif line.startswith(" "):
            # Context line
            current_hunk["lines"].append({
                "type": "context",
                "content": line[1:],
                "old_no": old_no,
                "new_no": new_no,
            })
            old_no += 1
            new_no += 1
        # Skip empty lines (artifact of split)

    # Don't forget the last file/hunk
    if current_file:
        if current_hunk:
            current_file["hunks"].append(current_hunk)
        files.append(current_file)

    return files


def get_file_with_gutters(commit_hash: str, file_path: str) -> dict:
    """Get full file content at a commit with gutter annotations.

    Returns dict with:
      content: raw file text
      lines: [{no, content, gutter: null|"added"|"deleted"|"modified", deleted_lines: [...]}]
    """
    h = _validate_hash(commit_hash)
    _validate_file_path(file_path)

    # Get the file content at this commit
    try:
        content = _run_git("show", f"{h}:{file_path}")
    except subprocess.CalledProcessError:
        raise FileNotFoundError(f"File {file_path} not found at commit {h}")

    # Get the diff for this specific file to compute gutters
    try:
        diff_output = _run_git("diff", f"{h}^..{h}", "--", file_path, check=False)
    except subprocess.CalledProcessError:
        diff_output = ""

    # Parse the diff to find which lines were added/modified/deleted
    gutter_data = _compute_gutters(diff_output, content)

    return {
        "content": content,
        "lines": gutter_data,
    }


def _validate_file_path(file_path: str) -> None:
    """Validate a file path to prevent command injection."""
    if ".." in file_path:
        raise ValueError("Path traversal not allowed")
    if file_path.startswith("/"):
        raise ValueError("Absolute paths not allowed")
    if not re.match(r"^[\w\-./]+$", file_path):
        raise ValueError(f"Invalid file path: {file_path!r}")


def _compute_gutters(diff_output: str, file_content: str) -> list[dict]:
    """Compute gutter annotations from a diff.

    Returns a list of line dicts with gutter markers and deleted line data.
    """
    file_lines = file_content.split("\n")
    # Remove trailing empty line from split if file ends with newline
    if file_lines and file_lines[-1] == "":
        file_lines = file_lines[:-1]

    # Initialize all lines with no gutter
    result = []
    for i, line_text in enumerate(file_lines):
        result.append({
            "no": i + 1,
            "content": line_text,
            "gutter": None,
            "deleted_lines": [],
        })

    if not diff_output.strip():
        return result

    # Parse diff hunks to find added/modified/deleted lines
    hunks = _parse_diff_hunks(diff_output)

    for hunk in hunks:
        _apply_hunk_gutters(hunk, result)

    return result


def _parse_diff_hunks(diff_output: str) -> list[dict]:
    """Parse diff output into hunks with old/new line info."""
    hunks = []
    current_hunk = None

    for line in diff_output.split("\n"):
        hunk_match = re.match(r"^@@\s+\-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@", line)
        if hunk_match:
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = {
                "old_start": int(hunk_match.group(1)),
                "new_start": int(hunk_match.group(2)),
                "changes": [],
            }
            continue

        if current_hunk is None:
            continue

        if line.startswith("+"):
            current_hunk["changes"].append(("add", line[1:]))
        elif line.startswith("-"):
            current_hunk["changes"].append(("del", line[1:]))
        elif line.startswith(" "):
            current_hunk["changes"].append(("ctx", line[1:]))
        elif line.startswith("\\"):
            continue

    if current_hunk:
        hunks.append(current_hunk)

    return hunks


def _apply_hunk_gutters(hunk: dict, lines: list[dict]) -> None:
    """Apply gutter markers from a single hunk to the line list.

    Logic:
    - Pure additions (no adjacent deletions): gutter = "added"
    - Pure deletions (no adjacent additions): gutter = "deleted" on nearest line,
      with deleted_lines populated
    - Replacements (deletions followed by additions): gutter = "modified"
    """
    changes = hunk["changes"]
    new_line_no = hunk["new_start"]

    i = 0
    while i < len(changes):
        change_type, content = changes[i]

        if change_type == "ctx":
            new_line_no += 1
            i += 1
            continue

        if change_type == "del":
            # Collect consecutive deletions
            deleted = []
            while i < len(changes) and changes[i][0] == "del":
                deleted.append(changes[i][1])
                i += 1

            # Check if followed by additions (replacement)
            added_count = 0
            while i < len(changes) and changes[i][0] == "add":
                # Mark as modified
                line_idx = new_line_no - 1
                if 0 <= line_idx < len(lines):
                    lines[line_idx]["gutter"] = "modified"
                    # Attach deleted lines to the first modified line
                    if added_count == 0:
                        lines[line_idx]["deleted_lines"] = deleted
                new_line_no += 1
                added_count += 1
                i += 1

            if added_count == 0:
                # Pure deletion — mark the line at this position
                line_idx = new_line_no - 1
                if 0 <= line_idx < len(lines):
                    lines[line_idx]["gutter"] = "deleted"
                    lines[line_idx]["deleted_lines"] = deleted
                elif lines:
                    # Deletion at end of file — mark last line
                    lines[-1]["gutter"] = "deleted"
                    lines[-1]["deleted_lines"] = deleted

        elif change_type == "add":
            # Pure addition
            line_idx = new_line_no - 1
            if 0 <= line_idx < len(lines):
                lines[line_idx]["gutter"] = "added"
            new_line_no += 1
            i += 1
