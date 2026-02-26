# /// script
# dependencies = []
# ///
"""Search Merlin's memory: knowledge base and daily logs."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # project root for paths module
import paths

MEMORY_DIR = paths.memory_dir()
KB_DIR = MEMORY_DIR / "kb"
LOGS_DIR = MEMORY_DIR / "logs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rg(pattern: str, path: Path, *, ignore_case: bool = True,
         context: int = 1) -> str:
    """Run ripgrep and return stdout. Returns empty string on no matches."""
    cmd = ["rg", "--no-heading", "-n", f"-C{context}"]
    if ignore_case:
        cmd.append("-i")
    cmd += [pattern, str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout


def _parse_frontmatter(path: Path) -> dict[str, str]:
    """Extract YAML frontmatter fields from a markdown file.

    Returns a dict with keys: title, created, tags, summary, related.
    Missing keys are empty strings.
    """
    text = path.read_text(errors="replace")
    fm: dict[str, str] = {}

    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return fm

    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip()
    return fm


def _format_kb_result(path: Path, snippet: str | None = None) -> str:
    """Format a single KB search result."""
    fm = _parse_frontmatter(path)
    title = fm.get("title", path.stem)
    summary = fm.get("summary", "")
    tags = fm.get("tags", "")

    lines = [f"**{title}** — `{path.name}`"]
    if summary:
        lines.append(f"  {summary}")
    if tags:
        lines.append(f"  Tags: {tags}")
    if snippet:
        lines.append(f"  ```\n  {snippet.strip()}\n  ```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# KB Search
# ---------------------------------------------------------------------------

def cmd_kb(args: argparse.Namespace) -> None:
    """Search the knowledge base."""
    if not KB_DIR.is_dir():
        print("Knowledge base directory not found.", file=sys.stderr)
        sys.exit(1)

    # Collect all .md files (exclude _index.md from keyword search results)
    kb_files = sorted(KB_DIR.glob("*.md"))
    if not kb_files:
        print("(no KB entries found)")
        return

    if args.tag:
        _kb_search_tag(kb_files, args.tag, discord=args.discord)
    elif args.keyword:
        _kb_search_keyword(args.keyword, discord=args.discord)
    else:
        # List all entries
        _kb_list(kb_files, discord=args.discord)


def _kb_list(files: list[Path], *, discord: bool = False) -> None:
    """List all KB entries with summaries."""
    results = []
    for f in files:
        if f.name == "_index.md":
            continue
        results.append(_format_kb_result(f))

    if not results:
        print("(no KB entries found)")
        return

    header = f"**Knowledge Base** ({len(results)} entries)"
    output = header + "\n\n" + "\n\n".join(results)
    print(output)


def _kb_search_tag(files: list[Path], tag: str, *, discord: bool = False) -> None:
    """Filter KB entries by tag."""
    tag_lower = tag.lower()
    results = []
    for f in files:
        if f.name == "_index.md":
            continue
        fm = _parse_frontmatter(f)
        tags_str = fm.get("tags", "").lower()
        if tag_lower in tags_str:
            results.append(_format_kb_result(f))

    if not results:
        print(f"(no KB entries with tag '{tag}')")
        return

    header = f"**KB entries tagged '{tag}'** ({len(results)} results)"
    output = header + "\n\n" + "\n\n".join(results)
    print(output)


def _kb_search_keyword(keyword: str, *, discord: bool = False) -> None:
    """Search KB content by keyword using ripgrep."""
    raw = _rg(keyword, KB_DIR, context=2)
    if not raw.strip():
        print(f"(no KB matches for '{keyword}')")
        return

    # Group results by file
    results: dict[str, list[str]] = {}
    current_file = None
    for line in raw.splitlines():
        # ripgrep outputs "path:line:content" or "path-line-content"
        match = re.match(r"^(.+\.md)[:\-]\d+[:\-](.*)", line)
        if match:
            filepath = match.group(1)
            content = match.group(2)
            if filepath != current_file:
                current_file = filepath
                results[filepath] = []
            results[filepath].append(content)
        elif line.strip() == "--":
            continue

    if not results:
        print(f"(no KB matches for '{keyword}')")
        return

    output_parts = [f"**KB search: '{keyword}'** ({len(results)} files)"]
    for filepath, snippets in results.items():
        path = Path(filepath)
        fm = _parse_frontmatter(path)
        title = fm.get("title", path.stem)
        snippet = "\n  ".join(snippets[:5])
        output_parts.append(f"\n**{title}** — `{path.name}`\n  {snippet}")

    print("\n".join(output_parts))


# ---------------------------------------------------------------------------
# Log Search
# ---------------------------------------------------------------------------

def cmd_log(args: argparse.Namespace) -> None:
    """Search daily logs."""
    if not LOGS_DIR.is_dir():
        print("Logs directory not found.", file=sys.stderr)
        sys.exit(1)

    # Determine date range
    date_from, date_to = _resolve_date_range(args)

    if args.keyword:
        _log_search_keyword(args.keyword, date_from, date_to, discord=args.discord)
    else:
        _log_list(date_from, date_to, discord=args.discord)


def _resolve_date_range(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Resolve --from/--to/--last into (from_date, to_date) strings."""
    if args.last:
        today = datetime.now()
        from_date = (today - timedelta(days=args.last)).strftime("%Y-%m-%d")
        to_date = today.strftime("%Y-%m-%d")
        return from_date, to_date
    return args.date_from, args.date_to


def _get_log_files(date_from: str | None, date_to: str | None) -> list[Path]:
    """Get log files filtered by date range."""
    all_logs = sorted(LOGS_DIR.glob("*.md"))

    if not date_from and not date_to:
        return all_logs

    filtered = []
    for f in all_logs:
        # Extract date from filename (YYYY-MM-DD.md)
        date_str = f.stem
        if date_from and date_str < date_from:
            continue
        if date_to and date_str > date_to:
            continue
        filtered.append(f)
    return filtered


def _log_list(date_from: str | None, date_to: str | None,
              *, discord: bool = False) -> None:
    """List available log files in date range."""
    files = _get_log_files(date_from, date_to)
    if not files:
        print("(no log files found)")
        return

    range_desc = ""
    if date_from or date_to:
        parts = []
        if date_from:
            parts.append(f"from {date_from}")
        if date_to:
            parts.append(f"to {date_to}")
        range_desc = f" ({' '.join(parts)})"

    lines = [f"**Daily logs{range_desc}** ({len(files)} files)"]
    for f in files:
        # Read first content line after the title
        text = f.read_text(errors="replace")
        entry_count = text.count("\n## ")
        lines.append(f"- `{f.name}` — {entry_count} entries")

    print("\n".join(lines))


def _log_search_keyword(keyword: str, date_from: str | None, date_to: str | None,
                        *, discord: bool = False) -> None:
    """Search log content by keyword."""
    files = _get_log_files(date_from, date_to)
    if not files:
        print("(no log files in range)")
        return

    all_matches: list[tuple[str, list[str]]] = []
    for f in files:
        raw = _rg(keyword, f, context=1)
        if raw.strip():
            matches = [
                line for line in raw.splitlines()
                if line.strip() and line.strip() != "--"
            ]
            if matches:
                all_matches.append((f.name, matches))

    if not all_matches:
        print(f"(no log matches for '{keyword}')")
        return

    range_desc = ""
    if date_from or date_to:
        parts = []
        if date_from:
            parts.append(f"from {date_from}")
        if date_to:
            parts.append(f"to {date_to}")
        range_desc = f" ({' '.join(parts)})"

    lines = [f"**Log search: '{keyword}'{range_desc}** ({len(all_matches)} files)"]
    for filename, matches in all_matches:
        lines.append(f"\n`{filename}`:")
        for m in matches[:10]:
            # Strip the filename prefix from rg output
            cleaned = re.sub(r"^.+\.md[:\-]\d+[:\-]\s*", "  ", m)
            lines.append(cleaned)

    print("\n".join(lines))


# ---------------------------------------------------------------------------
# Tag Index
# ---------------------------------------------------------------------------

def cmd_tags(args: argparse.Namespace) -> None:
    """List all tags used across the KB, with entry counts."""
    if not KB_DIR.is_dir():
        print("Knowledge base directory not found.", file=sys.stderr)
        sys.exit(1)

    tag_map: dict[str, list[str]] = {}  # tag -> [filenames]

    for f in sorted(KB_DIR.glob("*.md")):
        if f.name == "_index.md":
            continue
        fm = _parse_frontmatter(f)
        tags_str = fm.get("tags", "")
        # Parse [tag1, tag2] format
        cleaned = tags_str.strip("[] ")
        if not cleaned:
            continue
        for tag in cleaned.split(","):
            tag = tag.strip()
            if tag:
                tag_map.setdefault(tag, []).append(f.name)

    if not tag_map:
        print("(no tags found)")
        return

    lines = [f"**KB Tags** ({len(tag_map)} tags)\n"]
    for tag in sorted(tag_map):
        files = tag_map[tag]
        file_list = ", ".join(f"`{f}`" for f in files)
        lines.append(f"- **{tag}** ({len(files)}) — {file_list}")

    print("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search Merlin's memory (knowledge base and daily logs).",
        epilog="""
Examples:
  # List all KB entries
  uv run memory_search.py kb

  # Search KB by keyword
  uv run memory_search.py kb --keyword "docker"

  # Search KB by tag
  uv run memory_search.py kb --tag "project"

  # List all daily logs
  uv run memory_search.py log

  # Search logs from last 7 days
  uv run memory_search.py log --keyword "deployment" --last 7

  # Search logs in date range
  uv run memory_search.py log --keyword "error" --from 2026-01-01 --to 2026-01-31

  # List all tags used in the KB
  uv run memory_search.py tags

  # With Discord formatting
  uv run memory_search.py kb --keyword "music" --discord

Output:
  Results are printed to stdout as formatted text (markdown-ish).
  Use --discord for Discord-friendly formatting.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- kb subcommand ---
    kb_parser = subparsers.add_parser(
        "kb",
        help="Search the knowledge base",
        description="Search or list knowledge base entries. Without flags, lists all entries.",
    )
    kb_parser.add_argument("--keyword", "-k", help="Search KB content by keyword")
    kb_parser.add_argument("--tag", "-t", help="Filter by tag")
    kb_parser.add_argument("--discord", action="store_true",
                           help="Format output for Discord")
    kb_parser.set_defaults(func=cmd_kb)

    # --- log subcommand ---
    log_parser = subparsers.add_parser(
        "log",
        help="Search daily logs",
        description="Search or list daily memory logs. Without flags, lists all log files.",
    )
    log_parser.add_argument("--keyword", "-k", help="Search log content by keyword")
    log_parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD",
                            help="Start date (inclusive)")
    log_parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD",
                            help="End date (inclusive)")
    log_parser.add_argument("--last", type=int, metavar="N",
                            help="Search last N days")
    log_parser.add_argument("--discord", action="store_true",
                            help="Format output for Discord")
    log_parser.set_defaults(func=cmd_log)

    # --- tags subcommand ---
    tags_parser = subparsers.add_parser(
        "tags",
        help="List all KB tags",
        description="List all tags used across KB entries, with counts and file lists.",
    )
    tags_parser.set_defaults(func=cmd_tags)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
