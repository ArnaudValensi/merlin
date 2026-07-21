#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pyyaml"]
# ///
"""Merlin's knowledge base commands: add, index, check.

The KB is a flat directory of OKF-style notes: markdown with YAML
frontmatter, one concept per file. `type` is required; links live in the
body as normal markdown links, each carried by prose stating the
relationship. This command never writes links: relationship judgment
belongs to the author (usually an agent), the command validates and
maintains the mechanical parts (index, conformance).

Full format spec: docs/dev/notes-system.md.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root for paths
import paths
from argparse_help import HelpfulParser

paths.load_config_env()  # Honor config.env (e.g. NOTES_DIR) from any cwd

NOTES_DIR = paths.notes_dir()
KB_DIR = NOTES_DIR / "kb"
MEDIA_DIR = NOTES_DIR / "media"

# Reserved filenames are not concept notes (OKF reserved + our legacy index).
RESERVED_FILES = {"index.md", "log.md", "_index.md"}

OKF_VERSION = "0.1"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Markdown links: [label](target). Target captured up to ) or whitespace.
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")


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
    cleaned = tags_str.strip("[] ")
    if not cleaned:
        return []
    return [t.strip() for t in cleaned.split(",") if t.strip()]


def _normalize(value):
    """Keep frontmatter values string-friendly: dates become ISO strings."""
    import datetime

    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def read_frontmatter(path: Path) -> tuple[dict | None, str | None]:
    """Parse YAML frontmatter from a note file.

    Returns (meta, error). meta is None when there is no parseable
    frontmatter block; error carries the reason.
    """
    text = path.read_text(errors="replace")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None, "no frontmatter block"
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        return None, f"invalid YAML: {e}"
    if not isinstance(meta, dict):
        return None, "frontmatter is not a mapping"
    return {k: _normalize(v) for k, v in meta.items()}, None


def parse_frontmatter(path: Path) -> dict:
    """Frontmatter as a dict, {} when missing or unparseable."""
    meta, _ = read_frontmatter(path)
    return meta or {}


def note_body(path: Path) -> str:
    """Note content with the frontmatter block stripped."""
    text = path.read_text(errors="replace")
    match = FRONTMATTER_RE.match(text)
    return text[match.end() :] if match else text


def kb_notes() -> list[Path]:
    """All concept notes (reserved files excluded), sorted by name."""
    return sorted(f for f in KB_DIR.glob("*.md") if f.name not in RESERVED_FILES)


def corpus_vocab() -> tuple[Counter, Counter]:
    """(type counts, tag counts) across the KB."""
    types: Counter = Counter()
    tags: Counter = Counter()
    for f in kb_notes():
        meta = parse_frontmatter(f)
        note_type = str(meta.get("type") or "").strip()
        if note_type:
            types[note_type] += 1
        note_tags = meta.get("tags") or []
        if isinstance(note_tags, list):
            tags.update(str(t).strip() for t in note_tags if str(t).strip())
    return types, tags


def body_kb_links(body: str) -> list[tuple[str, str, str]]:
    """Markdown links to KB notes in a body: (label, target, line).

    Ignored: external links (with a scheme), non-.md targets, links inside
    fenced code blocks or inline code spans (notes about markdown quote
    example links), and targets with directory components (the KB is flat,
    so those point outside the bundle). Bundle-absolute targets (leading /)
    are resolved from the KB root.
    """
    # Join wrapped lines into logical lines so a list bullet's annotation
    # that continues on the next line stays attached to its link.
    logical: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        # An indented bullet is a nested item annotating its parent bullet.
        nested_bullet = line[
            : len(line) - len(line.lstrip())
        ] != "" and stripped.startswith(("-", "*"))
        is_continuation = (
            logical
            and stripped != ""
            and logical[-1].strip() != ""
            and (nested_bullet or not stripped.startswith(("-", "*", "#", ">")))
        )
        if is_continuation:
            logical[-1] += " " + stripped
        else:
            logical.append(line)

    links = []
    for line in logical:
        searchable = re.sub(r"`[^`]*`", "", line)  # drop inline code spans
        for match in MD_LINK_RE.finditer(searchable):
            label, target = match.group(1), match.group(2)
            if "://" in target or not target.split("#")[0].endswith(".md"):
                continue
            target = target.split("#")[0].lstrip("/")
            if "/" in target:
                continue
            links.append((label, target, line))
    return links


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def find_duplicates(title: str) -> list[tuple[str, str]]:
    """Check for potentially duplicate KB entries.

    Returns list of (filename, reason) for likely duplicates.
    """
    slug = slugify(title)
    duplicates = []
    for f in kb_notes():
        if f.stem == slug:
            duplicates.append((f.name, "exact filename match"))
            continue
        meta = parse_frontmatter(f)
        existing_title = str(meta.get("title") or "").lower()
        if existing_title and existing_title == title.lower():
            duplicates.append((f.name, "exact title match"))
    return duplicates


# ---------------------------------------------------------------------------
# Note creation
# ---------------------------------------------------------------------------


def yaml_value(value: str) -> str:
    """Quote a scalar for YAML when a plain rendering would misparse it."""
    needs_quoting = (
        ": " in value
        or value.endswith(":")
        or " #" in value
        or value != value.strip()
        or value.startswith(
            ("[", "{", "!", "&", "*", ">", "|", "%", "@", "`", '"', "'", "-", "?")
        )
    )
    if not needs_quoting:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_frontmatter(
    note_type: str,
    title: str,
    description: str,
    tags: list[str],
    resource: str | None,
) -> str:
    """Build the YAML frontmatter block, hand-formatted for a stable style."""
    today = datetime.now().strftime("%Y-%m-%d")
    tags_str = f"[{', '.join(tags)}]" if tags else "[]"
    lines = [
        "---",
        f"type: {note_type}",
        f"title: {yaml_value(title)}",
        f"description: {yaml_value(description)}",
        f"tags: {tags_str}",
    ]
    if resource:
        lines.append(f"resource: {yaml_value(resource)}")
    lines += [f"created: {today}", f"updated: {today}", "---"]
    return "\n".join(lines)


def create_note(
    note_type: str,
    title: str,
    description: str,
    tags: list[str],
    content: str,
    *,
    resource: str | None = None,
    filename: str | None = None,
) -> Path:
    """Create a new KB note file. Links are the author's job, not ours."""
    if filename is None:
        filename = slugify(title) + ".md"
    filepath = KB_DIR / filename
    frontmatter = build_frontmatter(note_type, title, description, tags, resource)
    filepath.write_text(f"{frontmatter}\n\n# {title}\n\n{content.strip()}\n")
    return filepath


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------


def build_index() -> str:
    """Render kb/index.md from note frontmatter, grouped by type."""
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for f in kb_notes():
        meta = parse_frontmatter(f)
        note_type = str(meta.get("type") or "").strip() or "(untyped)"
        title = str(meta.get("title") or f.stem)
        description = str(meta.get("description") or meta.get("summary") or "").strip()
        groups.setdefault(note_type, []).append((title, f.name, description))

    note_count = sum(len(v) for v in groups.values())
    typed = sorted(k for k in groups if k != "(untyped)")
    ordered = typed + (["(untyped)"] if "(untyped)" in groups else [])

    lines = [
        "---",
        f'okf_version: "{OKF_VERSION}"',
        "---",
        "",
        "# Knowledge Base Index",
        "",
        f"{note_count} notes across {len(typed)} types. "
        "Generated by `merlin kb index`; do not edit by hand.",
    ]
    for note_type in ordered:
        lines += ["", f"## {note_type}", ""]
        for title, name, description in sorted(groups[note_type]):
            entry = f"* [{title}]({name})"
            if description:
                entry += f" - {description}"
            lines.append(entry)
    return "\n".join(lines) + "\n"


def cmd_index(args: argparse.Namespace) -> None:
    """Regenerate kb/index.md."""
    if not KB_DIR.is_dir():
        print("Knowledge base directory not found.", file=sys.stderr)
        sys.exit(1)
    content = build_index()
    index_path = KB_DIR / "index.md"
    changed = not index_path.exists() or index_path.read_text() != content
    index_path.write_text(content)
    types, _ = corpus_vocab()
    status = "updated" if changed else "already current"
    print(
        f"**Index {status}:** `{index_path.name}` "
        f"({len(kb_notes())} notes, {len(types)} types)"
    )


# ---------------------------------------------------------------------------
# Conformance check
# ---------------------------------------------------------------------------


def check_findings() -> list[tuple[str, str]]:
    """Run all conformance checks. Returns (file, message) findings."""
    findings: list[tuple[str, str]] = []
    notes = kb_notes()
    note_names = {f.name for f in notes}
    resources: list[str] = []

    for f in notes:
        meta, error = read_frontmatter(f)
        if meta is None:
            findings.append((f.name, error or "unparseable frontmatter"))
            continue
        if not str(meta.get("type") or "").strip():
            findings.append((f.name, "missing required field: type"))
        if "related" in meta:
            findings.append(
                (f.name, "legacy field: related (links belong in the body)")
            )
        if "summary" in meta and "description" not in meta:
            findings.append((f.name, "legacy field: summary (rename to description)"))
        res = meta.get("resource")
        if isinstance(res, list):
            resources.extend(str(r) for r in res)
        elif res:
            resources.append(str(res))

        body = note_body(f)
        for label, target, line in body_kb_links(body):
            if target not in note_names:
                findings.append((f.name, f"broken link: ({target})"))
                continue
            # A link should be carried by prose: the line must say more
            # than the link itself.
            remainder = MD_LINK_RE.sub("", line).strip().strip("-*").strip()
            if len(remainder) < 5:
                findings.append(
                    (
                        f.name,
                        f"bare link to ({target}): state the relationship in prose",
                    )
                )

    # Legacy index file
    if (KB_DIR / "_index.md").exists():
        findings.append(
            ("_index.md", "legacy index file (replaced by generated index.md)")
        )

    # Generated index freshness
    index_path = KB_DIR / "index.md"
    if not index_path.exists():
        findings.append(("index.md", "missing (run `merlin kb index`)"))
    elif index_path.read_text() != build_index():
        findings.append(("index.md", "stale (run `merlin kb index`)"))

    # Media ownership: every media file referenced by some note's resource
    if MEDIA_DIR.is_dir():
        for media_file in sorted(MEDIA_DIR.iterdir()):
            if media_file.name.startswith("."):
                continue
            if not any(media_file.name in r for r in resources):
                findings.append(
                    (
                        f"media/{media_file.name}",
                        "orphaned: no note claims it via resource",
                    )
                )

    return findings


def cmd_check(args: argparse.Namespace) -> None:
    """Check KB conformance; exit 1 on findings."""
    if not KB_DIR.is_dir():
        print("Knowledge base directory not found.", file=sys.stderr)
        sys.exit(1)
    findings = check_findings()
    if not findings:
        print(f"**KB conforms** ({len(kb_notes())} notes, 0 findings)")
        return
    print(f"**KB check: {len(findings)} finding(s)**\n")
    for name, message in findings:
        print(f"- `{name}`: {message}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> None:
    """Add a new KB entry."""
    if not KB_DIR.is_dir():
        print("Knowledge base directory not found.", file=sys.stderr)
        sys.exit(1)

    note_type = args.type.strip()
    title = args.title
    tags = parse_tags(args.tags) if args.tags else []
    description = args.description or ""
    content = args.content or ""
    filename = (args.filename + ".md") if args.filename else None

    if not content and not sys.stdin.isatty():
        content = sys.stdin.read()
    if not content:
        print(
            "Error: --content is required (or pipe content via stdin).", file=sys.stderr
        )
        sys.exit(1)

    # --- Duplicates ---
    duplicates = find_duplicates(title)
    if duplicates:
        print("**Potential duplicates found:**")
        for dup_file, reason in duplicates:
            meta = parse_frontmatter(KB_DIR / dup_file)
            dup_title = meta.get("title", dup_file)
            print(f"  - `{dup_file}` — {dup_title} ({reason})")
        if not args.force:
            print(
                "\nUse --force to create anyway, or update the existing note instead."
            )
            sys.exit(1)
        print()

    # --- Link validation ---
    target_filename = filename or (slugify(title) + ".md")
    note_names = {f.name for f in kb_notes()}
    broken = [
        target
        for _, target, _ in body_kb_links(content)
        if target not in note_names and target != target_filename
    ]
    if broken:
        print("**Broken links** (targets not in the KB):")
        for target in broken:
            print(f"  - `{target}`")
        if not args.force:
            print("\nFix the links or use --force to create anyway.")
            sys.exit(1)
        print()

    # --- Vocabulary notices (informative, never blocking) ---
    types, tag_counts = corpus_vocab()
    notices = []
    if note_type not in types:
        existing = ", ".join(f"{t} ({n})" for t, n in types.most_common()) or "(none)"
        notices.append(f"**New type** `{note_type}`. Existing types: {existing}")
    novel_tags = [t for t in tags if t not in tag_counts]
    if novel_tags:
        novel = ", ".join(f"`{t}`" for t in novel_tags)
        notices.append(
            f"**New tag(s)** {novel}. Check `merlin notes search tags` for the "
            "existing vocabulary; keep new tags deliberate."
        )

    if args.dry_run:
        print(f"**Dry run — would create:** `{target_filename}`\n")
        print(f"**Type:** {note_type}")
        print(f"**Title:** {title}")
        print(f"**Tags:** {', '.join(tags) if tags else '(none)'}")
        print(f"**Description:** {description or '(none)'}")
        if args.resource:
            print(f"**Resource:** {args.resource}")
        print(f"**Content length:** {len(content)} chars")
        for notice in notices:
            print(f"\n{notice}")
        return

    # --- Create + refresh index ---
    filepath = create_note(
        note_type,
        title,
        description,
        tags,
        content,
        resource=args.resource,
        filename=filename,
    )
    (KB_DIR / "index.md").write_text(build_index())

    print(f"**Created:** `{filepath.name}`")
    print(f"**Type:** {note_type}")
    if tags:
        print(f"**Tags:** {', '.join(tags)}")
    print("**Index refreshed.**")
    for notice in notices:
        print(f"\n{notice}")
    if not body_kb_links(content):
        print(
            "\nNo links in the body. Fine for a standalone note; if a related "
            "note exists in the index, consider adding a prose link stating "
            "the relationship."
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = HelpfulParser(
        prog="merlin kb",
        description="Merlin's knowledge base (OKF-style Zettelkasten).",
        epilog="Also available as: merlin notes kb",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    add_parser = subparsers.add_parser(
        "add",
        help="Add a KB entry",
        description="Add an entry to the knowledge base.",
        epilog="""
Examples:
  # Add a note (type is required; pick tags from the existing vocabulary)
  merlin kb add --type technique --title "Docker Compose Tips" \\
    --tags "devops, docker" \\
    --description "Useful patterns for docker-compose" \\
    --content "Use volumes for persistent data..."

  # Pipe long content via stdin; point resource at the source
  cat transcript.md | merlin kb add --type transcript \\
    --title "Talk Notes" --tags "ai" \\
    --resource "https://youtube.com/watch?v=..."

  # Preview without creating
  merlin kb add --type tool --title "Mechanical Keyboards" \\
    --tags "gear" --content "..." --dry-run

Links are YOUR job, not this command's: put markdown links in the body
prose, each with the relationship stated ("Extends [X](x.md) by...").
The command validates link targets and refreshes index.md; it never
invents links.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_parser.add_argument(
        "--type",
        required=True,
        help="Kind of note (e.g. company, technique, tool, transcript, decision, reference)",
    )
    add_parser.add_argument(
        "--title", "-t", required=True, help="Note title (also generates the filename)"
    )
    add_parser.add_argument(
        "--tags", "-T", help="Comma-separated topic tags (e.g. 'music, gear')"
    )
    add_parser.add_argument(
        "--description",
        "-s",
        "--summary",
        dest="description",
        help="One-line description for the index and search results",
    )
    add_parser.add_argument(
        "--resource", "-r", help="URL or media/ path of the source asset"
    )
    add_parser.add_argument("--content", "-c", help="Note content (or pipe via stdin)")
    add_parser.add_argument(
        "--filename", "-f", help="Override auto-generated filename (without .md)"
    )
    add_parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview what would be created (no file changes)",
    )
    add_parser.add_argument(
        "--force",
        action="store_true",
        help="Create despite duplicates or broken links",
    )
    add_parser.set_defaults(func=cmd_add)

    index_parser = subparsers.add_parser(
        "index",
        help="Regenerate kb/index.md from note frontmatter",
        description="Regenerate the KB index (grouped by type). Deterministic; safe to run anytime.",
    )
    index_parser.set_defaults(func=cmd_index)

    check_parser = subparsers.add_parser(
        "check",
        help="Check KB conformance (exit 1 on findings)",
        description=(
            "Conformance check: frontmatter parses, type present, links resolve "
            "and carry prose, index fresh, media owned. The executable format spec."
        ),
    )
    check_parser.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
