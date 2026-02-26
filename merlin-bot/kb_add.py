# /// script
# dependencies = []
# ///
"""Add entries to Merlin's knowledge base with automatic link discovery.

Creates atomic KB notes following the Zettelkasten method:
- Searches for duplicate/similar notes before creating
- Finds related notes by keyword and tag overlap
- Generates proper frontmatter with links to related notes
- Updates related notes to link back (bidirectional linking)

All research (searching, matching) happens inside this script
to avoid polluting the caller's context.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # project root for paths module
import paths

MEMORY_DIR = paths.memory_dir()
KB_DIR = MEMORY_DIR / "kb"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """Convert text to a filename-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def parse_tags(tags_str: str) -> list[str]:
    """Parse a comma-separated or YAML-style tag string into a list."""
    # Handle YAML array format: [tag1, tag2]
    cleaned = tags_str.strip("[] ")
    if not cleaned:
        return []
    return [t.strip() for t in cleaned.split(",") if t.strip()]


def parse_frontmatter(path: Path) -> dict[str, str]:
    """Extract YAML frontmatter fields from a markdown file."""
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


def rg_search(pattern: str, path: Path) -> list[str]:
    """Run ripgrep and return matching file paths."""
    cmd = ["rg", "-l", "-i", pattern, str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [p.strip() for p in result.stdout.strip().splitlines() if p.strip()]


# ---------------------------------------------------------------------------
# Related-note discovery
# ---------------------------------------------------------------------------

def find_related_notes(title: str, tags: list[str], content: str,
                       exclude_file: str | None = None) -> dict[str, dict]:
    """Find KB notes related to the new entry.

    Searches by:
    1. Title words appearing in other notes
    2. Tag overlap
    3. Content keyword matches

    Returns {filename: {title, tags, summary, score, reasons}} sorted by score.
    """
    kb_files = [f for f in KB_DIR.glob("*.md")
                if f.name != "_index.md" and f.name != exclude_file]

    if not kb_files:
        return {}

    # Build score map: filename -> {score, reasons}
    scores: dict[str, dict] = {}

    def _add_score(path: Path, points: int, reason: str):
        name = path.name
        if name not in scores:
            fm = parse_frontmatter(path)
            scores[name] = {
                "title": fm.get("title", path.stem),
                "tags": fm.get("tags", ""),
                "summary": fm.get("summary", ""),
                "score": 0,
                "reasons": [],
            }
        scores[name]["score"] += points
        scores[name]["reasons"].append(reason)

    # 1. Tag overlap — strongest signal
    if tags:
        for f in kb_files:
            fm = parse_frontmatter(f)
            file_tags = parse_tags(fm.get("tags", ""))
            overlap = set(t.lower() for t in tags) & set(t.lower() for t in file_tags)
            if overlap:
                _add_score(f, 3 * len(overlap),
                           f"shared tags: {', '.join(overlap)}")

    # 2. Title word matches — check if significant title words appear in other notes
    title_words = [w.lower() for w in re.findall(r"\w{4,}", title)]
    for word in title_words:
        matching_files = rg_search(word, KB_DIR)
        for filepath in matching_files:
            path = Path(filepath)
            if path.name == "_index.md" or path.name == exclude_file:
                continue
            if path in kb_files:
                _add_score(path, 2, f"title word '{word}' found in content")

    # 3. Content keyword matches — extract significant words from content
    content_words = set(w.lower() for w in re.findall(r"\w{5,}", content))
    # Limit to avoid excessive rg calls
    sample_words = sorted(content_words - set(title_words))[:10]
    for word in sample_words:
        matching_files = rg_search(word, KB_DIR)
        for filepath in matching_files:
            path = Path(filepath)
            if path.name == "_index.md" or path.name == exclude_file:
                continue
            if path in kb_files:
                _add_score(path, 1, f"content word '{word}' found")

    # Sort by score descending, return top results
    sorted_scores = dict(sorted(scores.items(),
                                key=lambda x: x[1]["score"], reverse=True))
    return sorted_scores


def find_duplicates(title: str, tags: list[str]) -> list[tuple[str, str]]:
    """Check for potentially duplicate KB entries.

    Returns list of (filename, reason) for likely duplicates.
    """
    slug = slugify(title)
    duplicates = []

    for f in KB_DIR.glob("*.md"):
        if f.name == "_index.md":
            continue

        # Exact filename match
        if f.stem == slug:
            duplicates.append((f.name, "exact filename match"))
            continue

        # Similar title
        fm = parse_frontmatter(f)
        existing_title = fm.get("title", "").lower()
        if existing_title and existing_title == title.lower():
            duplicates.append((f.name, "exact title match"))

    return duplicates


# ---------------------------------------------------------------------------
# Note creation
# ---------------------------------------------------------------------------

def build_frontmatter(title: str, tags: list[str], summary: str,
                      related: list[str]) -> str:
    """Build YAML frontmatter string."""
    created = datetime.now().strftime("%Y-%m-%d")
    tags_str = f"[{', '.join(tags)}]" if tags else "[]"
    related_str = f"[{', '.join(related)}]" if related else "[]"

    return f"""---
title: {title}
created: {created}
tags: {tags_str}
related: {related_str}
summary: {summary}
---"""


def update_related_note(note_path: Path, new_file: str) -> bool:
    """Add a backlink to an existing note's related field.

    Returns True if the note was updated.
    """
    text = note_path.read_text(errors="replace")

    match = re.match(r"^(---\s*\n)(.*?)(\n---)", text, re.DOTALL)
    if not match:
        return False

    fm_content = match.group(2)

    # Find the related: line
    related_match = re.search(r"^(related:\s*)\[([^\]]*)\]", fm_content, re.MULTILINE)
    if not related_match:
        # No related field — add one
        new_fm = fm_content + f"\nrelated: [{new_file}]"
        new_text = match.group(1) + new_fm + match.group(3) + text[match.end():]
        note_path.write_text(new_text)
        return True

    existing = related_match.group(2).strip()
    if new_file in existing:
        return False  # Already linked

    if existing:
        new_related = f"{existing}, {new_file}"
    else:
        new_related = new_file

    new_line = f"{related_match.group(1)}[{new_related}]"
    new_fm = fm_content[:related_match.start()] + new_line + fm_content[related_match.end():]
    new_text = match.group(1) + new_fm + match.group(3) + text[match.end():]
    note_path.write_text(new_text)
    return True


def create_note(title: str, tags: list[str], summary: str,
                content: str, related: list[str], *,
                filename: str | None = None) -> Path:
    """Create a new KB note file."""
    if filename is None:
        filename = slugify(title) + ".md"

    filepath = KB_DIR / filename
    frontmatter = build_frontmatter(title, tags, summary, related)

    note_content = f"""{frontmatter}

# {title}

{content.strip()}
"""

    # Add See Also section if there are related notes
    if related:
        note_content += "\n## See Also\n"
        for rel in related:
            rel_path = KB_DIR / rel
            if rel_path.exists():
                fm = parse_frontmatter(rel_path)
                rel_title = fm.get("title", rel_path.stem)
            else:
                rel_title = Path(rel).stem
            note_content += f"- [{rel_title}]({rel})\n"

    filepath.write_text(note_content)
    return filepath


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_add(args: argparse.Namespace) -> None:
    """Add a new KB entry with automatic link discovery."""
    if not KB_DIR.is_dir():
        print("Knowledge base directory not found.", file=sys.stderr)
        sys.exit(1)

    title = args.title
    tags = parse_tags(args.tags) if args.tags else []
    summary = args.summary or ""
    content = args.content or ""
    filename = (args.filename + ".md") if args.filename else None

    # Read content from stdin if not provided
    if not content and not sys.stdin.isatty():
        content = sys.stdin.read()

    if not content:
        print("Error: --content is required (or pipe content via stdin).",
              file=sys.stderr)
        sys.exit(1)

    # --- Check for duplicates ---
    duplicates = find_duplicates(title, tags)
    if duplicates:
        print("**Potential duplicates found:**")
        for dup_file, reason in duplicates:
            fm = parse_frontmatter(KB_DIR / dup_file)
            dup_title = fm.get("title", dup_file)
            print(f"  - `{dup_file}` — {dup_title} ({reason})")

        if not args.force:
            print("\nUse --force to create anyway, or update the existing note instead.")
            sys.exit(1)
        print()

    # --- Find related notes ---
    target_filename = filename or (slugify(title) + ".md")
    related = find_related_notes(title, tags, content, exclude_file=target_filename)

    # Pick top related notes (score >= 2)
    related_files = [name for name, info in related.items() if info["score"] >= 2]
    # Cap at 8 related notes
    related_files = related_files[:8]

    if args.dry_run:
        print(f"**Dry run — would create:** `{target_filename}`\n")
        print(f"**Title:** {title}")
        print(f"**Tags:** {', '.join(tags) if tags else '(none)'}")
        print(f"**Summary:** {summary or '(none)'}")
        print(f"**Content length:** {len(content)} chars")

        if related_files:
            print(f"\n**Related notes** ({len(related_files)} found):")
            for name in related_files:
                info = related[name]
                reasons = "; ".join(info["reasons"][:3])
                print(f"  - `{name}` — {info['title']} (score: {info['score']}, {reasons})")
        else:
            print("\n**Related notes:** (none found)")

        if related and not related_files:
            weak = list(related.items())[:3]
            if weak:
                print("\n**Weak matches** (score < 2):")
                for name, info in weak:
                    print(f"  - `{name}` — {info['title']} (score: {info['score']})")
        return

    # --- Create the note ---
    filepath = create_note(title, tags, summary, content, related_files,
                           filename=filename)

    # --- Update backlinks on related notes ---
    backlinked = []
    for rel_name in related_files:
        rel_path = KB_DIR / rel_name
        if rel_path.exists() and update_related_note(rel_path, filepath.name):
            backlinked.append(rel_name)

    # --- Output summary ---
    print(f"**Created:** `{filepath.name}`")
    print(f"**Title:** {title}")
    if tags:
        print(f"**Tags:** {', '.join(tags)}")
    if related_files:
        print(f"**Linked to:** {', '.join(f'`{r}`' for r in related_files)}")
    if backlinked:
        print(f"**Backlinks added to:** {', '.join(f'`{b}`' for b in backlinked)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add entries to Merlin's knowledge base with automatic link discovery.",
        epilog="""
Examples:
  # Add a simple note
  uv run kb_add.py --title "Docker Compose Tips" \\
    --tags "devops, docker" \\
    --summary "Useful patterns for docker-compose" \\
    --content "Use volumes for persistent data..."

  # Pipe content from a file or command
  echo "Long article content..." | uv run kb_add.py \\
    --title "Article Notes" --tags "reading"

  # Preview without creating (shows related notes found)
  uv run kb_add.py --title "Mechanical Keyboards" \\
    --tags "tech, gear" --content "..." --dry-run

  # Force create even if duplicate detected
  uv run kb_add.py --title "Docker Setup" \\
    --tags "devops" --content "..." --force

How it works:
  1. Checks for duplicate notes (same title or filename)
  2. Searches KB for related notes by tag overlap, title words, content keywords
  3. Creates the note with frontmatter linking to related notes
  4. Updates related notes with backlinks to the new note
  5. Prints a summary of what was created and linked

The search happens entirely inside this script to keep the caller's
context clean (Zettelkasten link discovery can be token-heavy).
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--title", "-t", required=True,
                        help="Note title (also used to generate filename)")
    parser.add_argument("--tags", "-T",
                        help="Comma-separated tags (e.g. 'music, gear, shopping')")
    parser.add_argument("--summary", "-s",
                        help="One-line summary for quick scanning")
    parser.add_argument("--content", "-c",
                        help="Note content (or pipe via stdin)")
    parser.add_argument("--filename", "-f",
                        help="Override auto-generated filename (without .md)")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Preview what would be created (no file changes)")
    parser.add_argument("--force", action="store_true",
                        help="Create even if duplicate detected")

    args = parser.parse_args()
    cmd_add(args)


if __name__ == "__main__":
    main()
