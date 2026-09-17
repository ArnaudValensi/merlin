"""``merlin review``: the agent's side of a code review.

The user reviews the agent's work on the Commits page and leaves comments.
This command reads those reviews, answers and resolves the threads, and
prints markdown meant for an agent's context. It works on the same store as
the page (``commits/reviews.py``, direct file access under the same lock, no
HTTP), so the page shows every change within its next poll.

Subcommands: list, show, diff, comment, reply, resolve, reopen, close,
reopen-review. The default author is ``agent``. ``--author user`` is there
because nothing stops the user from using the CLI too.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Run as a subprocess or through cli.py: both need the repo root on the path.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import paths  # noqa: E402
from argparse_help import HelpfulParser  # noqa: E402
from commits import compare as cm  # noqa: E402
from commits import reviews as rv  # noqa: E402
from commits.git_parser import _find_repo_root  # noqa: E402


class CliError(Exception):
    """A user-facing error: printed on stderr, exit 1."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_root(repo: str | None) -> Path:
    """``--repo`` or the git root of the current directory."""
    start = repo or "."
    if not Path(start).is_dir():
        raise CliError(f"Not a directory: {start}")
    root = _find_repo_root(start)
    if root is None:
        raise CliError(f"Not a git repository: {Path(start).resolve()}")
    return root.resolve()


def _review_repo(review: dict) -> Path:
    """The review's own repository, which must still be exactly a root."""
    repo = review.get("repo") or ""
    p = Path(repo)
    if not repo or not p.is_dir():
        raise CliError(f"Repository not found: {repo}")
    root = _find_repo_root(str(p))
    if root is None or root.resolve() != p.resolve():
        raise CliError(f"Repository not found: {repo}")
    return root


def _load(review_id: str) -> dict:
    try:
        rv.validate_id(review_id)
        return rv.load(review_id)
    except ValueError as e:
        raise CliError(str(e))
    except rv.ReviewNotFound:
        raise CliError(f"Unknown review: {review_id}")
    except rv.ReviewCorrupt as e:
        raise CliError(str(e))


def _when(stamp: str | None) -> str:
    """An ISO stamp to the minute, in UTC, for the agent's context."""
    if not stamp:
        return ""
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _body_arg(words: list[str]) -> str:
    """The comment body: the words joined, or stdin when the body is ``-``."""
    text = " ".join(words).strip()
    if text == "-":
        text = sys.stdin.read()
    return text


def _short(sha: str | None) -> str:
    return sha[:7] if sha else ""


def _refs_line(cmp: cm.Comparison, count: int) -> str:
    if cmp.worktree:
        return f"Working tree against {cmp.base} ({_short(cmp.base_resolved)})"
    parts = [
        f"{cmp.base} ({_short(cmp.base_resolved)}) .. {cmp.head} ({_short(cmp.head_resolved)})"
    ]
    if cmp.mergebase:
        parts.append(f"merge base {_short(cmp.merge_base)}")
    parts.append(f"{count} commit{'' if count == 1 else 's'}")
    return " · ".join(parts)


def _quote(text: str) -> str:
    return "\n".join("> " + line for line in text.split("\n"))


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.split("\n"))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_list(reviews: list[dict]) -> str:
    if not reviews:
        return "No reviews.\n"
    lines = []
    for r in reviews:
        if r.get("error"):
            lines.append(f"- {r['id']}  (unreadable: {r['error']})")
            continue
        open_n = r.get("open_comments", 0)
        lines.append(
            f"- {r['id']}  {r.get('title', '')}  · {r.get('kind')}"
            f"  · {r.get('status')}  · {open_n} open thread{'' if open_n == 1 else 's'}"
            f"  · updated {_when(r.get('updated'))}"
        )
    return "\n".join(lines) + "\n"


def render_thread(comment: dict, state: str | None) -> str:
    cid = comment["id"]
    if comment.get("path"):
        where = f"{comment['path']}:{comment.get('line')} ({comment.get('side')})"
    else:
        where = "review-wide"
    label = ""
    if state in ("moved", "outdated"):
        label = f" [{state}]"
    if comment.get("status") == "resolved":
        label += f" [resolved by {comment.get('resolved_by')}]"
    out = [
        f"### {cid} · {where} · {comment.get('author')} · {_when(comment.get('created'))}{label}"
    ]
    if comment.get("line_text") is not None:
        out.append(_quote(comment["line_text"]))
    out.append(comment.get("body", ""))
    for r in comment.get("replies") or []:
        out.append(f"- {r.get('author')} · {_when(r.get('created'))}:")
        out.append(_indent(r.get("body", "")))
    return "\n".join(out) + "\n"


def render_show(
    review: dict,
    cmp: cm.Comparison | None,
    files: list[dict],
    count: int,
    new_commits: int,
    anchors: dict[str, str],
    show_all: bool,
    error: str | None = None,
) -> str:
    out = [f"# {review.get('title', '')} ({review['id']})"]
    out.append(
        f"Repo: {review.get('repo')} · Kind: {review.get('kind')} · Status: {review.get('status')}"
    )
    if cmp is not None:
        out.append(f"Refs: {_refs_line(cmp, count)}")
    else:
        out.append(f"Refs: {review.get('base')} .. {review.get('head')}")
    if error:
        out.append(f"Error: {error}")
    if new_commits:
        out.append(
            f"{new_commits} new commit{'' if new_commits == 1 else 's'} on the head since the user last looked."
        )
    out.append("")

    viewed = review.get("files") or {}
    counts = rv.open_thread_counts(review)
    if files:
        out.append(
            f"## Files ({len([f for f in files if f['path'] in viewed])} of {len(files)} viewed)"
        )
        for f in files:
            mark = "x" if f["path"] in viewed else " "
            stats = f"+{f.get('insertions', 0)} -{f.get('deletions', 0)}"
            n = counts.get(f["path"], 0)
            threads = f" · {n} open thread{'' if n == 1 else 's'}" if n else ""
            out.append(
                f"- [{mark}] {f['path']} ({f.get('status', '')}, {stats}){threads}"
            )
        out.append("")

    comments = review.get("comments") or []
    open_threads = [c for c in comments if c.get("status") == "open"]
    resolved = [c for c in comments if c.get("status") != "open"]
    out.append(f"## Open threads ({len(open_threads)})")
    if not open_threads:
        out.append("None.")
    for c in open_threads:
        out.append(render_thread(c, anchors.get(c["id"])))
    if resolved:
        if show_all:
            out.append(f"## Resolved threads ({len(resolved)})")
            for c in resolved:
                out.append(render_thread(c, anchors.get(c["id"])))
        else:
            out.append(f"Resolved threads: {len(resolved)} (use --all to include them)")
    out.append("")
    out.append(
        "Reply with `merlin review reply <review> <thread> <text>`, resolve with "
        "`merlin review resolve <review> <thread> [-m <text>]`."
    )
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(args) -> int:
    root = _repo_root(args.repo)
    reviews = rv.list_for_repo(str(root))
    if not args.all:
        reviews = [r for r in reviews if r.get("error") or r.get("status") != "closed"]
    if args.json:
        print(json.dumps(reviews, indent=2))
    else:
        sys.stdout.write(render_list(reviews))
    return 0


def cmd_show(args) -> int:
    review = _load(args.review)
    error = None
    cmp = None
    files: list[dict] = []
    count = 0
    new_commits = 0
    anchors: dict[str, str] = {}
    repo_dir: Path | None = None
    try:
        # A repository that moved or a branch that is gone is an error next
        # to the record, never a reason to hide the threads: the agent still
        # needs their ids to reply and resolve.
        repo_dir = _review_repo(review)
        # The agent's look is not the user's: last_seen_head stays.
        loaded = rv.refresh(args.review, repo_dir, advance_seen=False)
        review = loaded.review
        cmp = loaded.cmp
        new_commits = loaded.new_commits
        anchors = loaded.anchors
        files = cm.compare_files(cmp, repo_dir)
        _, count, _ = cm.compare_commits(cmp, repo_dir)
    except (cm.RefError, CliError) as e:
        error = str(e)
    if args.json:
        payload = {
            "review": review,
            "comparison": cm.compare_detail(cmp, repo_dir)
            if cmp and repo_dir
            else None,
            "new_commits": new_commits,
            "anchors": anchors,
            "open_threads": rv.open_thread_counts(review),
            "error": error,
        }
        print(json.dumps(payload, indent=2))
        return 0
    sys.stdout.write(
        render_show(review, cmp, files, count, new_commits, anchors, args.all, error)
    )
    return 0


def cmd_diff(args) -> int:
    review = _load(args.review)
    repo_dir = _review_repo(review)
    try:
        cmp = rv.comparison_of(review, repo_dir)
        sys.stdout.write(cm.compare_patch(cmp, repo_dir, args.path))
    except cm.RefError as e:
        raise CliError(str(e))
    except ValueError as e:
        raise CliError(str(e))
    return 0


def cmd_comment(args) -> int:
    review = _load(args.review)
    repo_dir = _review_repo(review)
    if (args.path is None) != (args.line is None):
        raise CliError("A line comment needs both --path and --line")
    try:
        _review, comment = rv.add_comment(
            args.review,
            repo_dir,
            body=_body_arg(args.body),
            author=args.author,
            path=args.path,
            side=args.side,
            line=args.line,
        )
    except (ValueError, cm.RefError) as e:
        raise CliError(str(e))
    print(f"Commented {comment['id']} on {args.review}")
    return 0


def cmd_reply(args) -> int:
    _load(args.review)
    try:
        rv.reply(args.review, args.comment, _body_arg(args.body), args.author)
    except KeyError as e:
        raise CliError(str(e.args[0]))
    except ValueError as e:
        raise CliError(str(e))
    print(f"Replied on {args.comment}")
    return 0


def cmd_resolve(args) -> int:
    _load(args.review)
    try:
        rv.resolve(args.review, args.comment, args.author, args.message)
    except KeyError as e:
        raise CliError(str(e.args[0]))
    except ValueError as e:
        raise CliError(str(e))
    print(f"Resolved {args.comment}")
    return 0


def cmd_reopen(args) -> int:
    _load(args.review)
    try:
        rv.reopen(args.review, args.comment)
    except KeyError as e:
        raise CliError(str(e.args[0]))
    print(f"Reopened {args.comment}")
    return 0


def cmd_close(args) -> int:
    _load(args.review)
    rv.set_status(args.review, "closed")
    print(f"Closed review {args.review}")
    return 0


def cmd_reopen_review(args) -> int:
    _load(args.review)
    rv.set_status(args.review, "open")
    print(f"Reopened review {args.review}")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser(prog: str = "merlin review") -> argparse.ArgumentParser:
    parser = HelpfulParser(
        prog=prog,
        description=(
            "Read and answer the code reviews the user leaves on the Commits page. "
            "Output is markdown for an agent's context."
        ),
        epilog="""
Examples:
  merlin review list                         open reviews of this repository
  merlin review show 3f9a1c2d                the review, its files, its open threads
  merlin review diff 3f9a1c2d --path a.py    the comparison's diff as git prints it
  merlin review reply 3f9a1c2d 8b1c2d3e "Done, renamed in 4a5b6c7."
  merlin review resolve 3f9a1c2d 8b1c2d3e -m "Fixed."
  merlin review comment 3f9a1c2d --path a.py --line 12 "Why the retry here?"

The store is ~/.merlin/reviews/, one JSON file per review, shared with the
Commits page. The default author is agent.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    def author(p):
        p.add_argument(
            "--author",
            choices=list(rv.AUTHORS),
            default="agent",
            help="who speaks (default: agent)",
        )

    p = sub.add_parser("list", help="Open reviews of the repository")
    p.add_argument("--all", action="store_true", help="include closed reviews")
    p.add_argument(
        "--repo", help="repository (default: the git root of the current directory)"
    )
    p.add_argument("--json", action="store_true", help="raw records")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="A review: refs, files, open threads")
    p.add_argument("review", help="review id")
    p.add_argument("--all", action="store_true", help="include resolved threads")
    p.add_argument("--json", action="store_true", help="the raw record and comparison")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("diff", help="The comparison's unified diff")
    p.add_argument("review", help="review id")
    p.add_argument("--path", help="one file only")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("comment", help="A new thread on a line, or review-wide")
    p.add_argument("review", help="review id")
    p.add_argument("--path", help="file path (with --line)")
    p.add_argument("--line", type=int, help="line number on that side")
    p.add_argument(
        "--side", choices=list(rv.SIDES), default="new", help="new (default) or old"
    )
    author(p)
    p.add_argument("body", nargs="+", help="the comment (or - for stdin)")
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("reply", help="Reply on a thread")
    p.add_argument("review", help="review id")
    p.add_argument("comment", help="thread id")
    author(p)
    p.add_argument("body", nargs="+", help="the reply (or - for stdin)")
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("resolve", help="Resolve a thread")
    p.add_argument("review", help="review id")
    p.add_argument("comment", help="thread id")
    p.add_argument("-m", "--message", help="a closing reply")
    author(p)
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("reopen", help="Reopen a resolved thread")
    p.add_argument("review", help="review id")
    p.add_argument("comment", help="thread id")
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("close", help="Close a review")
    p.add_argument("review", help="review id")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("reopen-review", help="Reopen a closed review")
    p.add_argument("review", help="review id")
    p.set_defaults(func=cmd_reopen_review)
    return parser


def main(argv: list[str] | None = None, prog: str = "merlin review") -> int:
    paths.load_config_env()
    parser = build_parser(prog)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"error: git failed: {e.stderr or e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], prog="review_cli.py"))
