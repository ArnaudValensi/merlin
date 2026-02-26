# /// script
# dependencies = []
# ///
"""Add facts to Merlin's user memory (memory/user.md).

Appends facts to the appropriate section of user.md. Facts are short,
durable things about the user — preferences, identity, context.

For longer knowledge entries, use kb_add.py instead.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # project root for paths module
import paths

MEMORY_DIR = paths.memory_dir()
USER_MD = MEMORY_DIR / "user.md"

# Sections in user.md (lowercase for matching)
KNOWN_SECTIONS = ["identity", "preferences", "context", "notes"]


def get_sections(text: str) -> dict[str, tuple[int, int]]:
    """Parse user.md and return section name -> (start_line, end_line).

    Lines are 0-indexed. end_line is exclusive (points to next section header
    or end of file).
    """
    lines = text.splitlines()
    sections: dict[str, tuple[int, int]] = {}
    current_section = None
    current_start = 0

    for i, line in enumerate(lines):
        match = re.match(r"^## (.+)", line)
        if match:
            if current_section is not None:
                sections[current_section] = (current_start, i)
            current_section = match.group(1).strip().lower()
            current_start = i

    if current_section is not None:
        sections[current_section] = (current_start, len(lines))

    return sections


def add_fact(fact: str, section: str | None = None) -> str:
    """Add a fact to user.md.

    If section is provided, appends to that section.
    Otherwise appends to "Notes".

    Returns a message describing what was done.
    """
    if not USER_MD.exists():
        return f"Error: {USER_MD} not found."

    text = USER_MD.read_text()
    lines = text.splitlines()

    sections = get_sections(text)

    # Determine target section
    target = (section or "notes").lower()
    if target not in sections:
        # Try fuzzy match
        for s in sections:
            if target in s or s in target:
                target = s
                break
        else:
            target = "notes"

    start, end = sections[target]

    # Format the fact as a bullet point if not already
    fact_line = fact.strip()
    if not fact_line.startswith("- "):
        fact_line = f"- {fact_line}"

    # Find insertion point: after last non-empty line in section
    insert_at = end
    for i in range(end - 1, start, -1):
        if lines[i].strip():
            insert_at = i + 1
            break

    # Remove placeholder text if present
    section_content = "\n".join(lines[start + 1:end])
    if "(to be filled in)" in section_content or "(Add important facts" in section_content:
        # Collect non-placeholder, non-empty lines from section body
        new_lines = []
        for i in range(start + 1, end):
            stripped = lines[i].strip()
            if not stripped:
                continue
            if "(to be filled in)" in stripped or "(Add important facts" in stripped:
                continue
            new_lines.append(lines[i])

        # Rebuild: header + blank + kept lines + new fact + blank + rest
        result_lines = lines[:start + 1]
        result_lines.append("")
        if new_lines:
            result_lines.extend(new_lines)
        result_lines.append(fact_line)
        result_lines.append("")
        result_lines.extend(lines[end:])
    else:
        # Normal append
        result_lines = lines[:insert_at]
        result_lines.append(fact_line)
        result_lines.extend(lines[insert_at:])

    USER_MD.write_text("\n".join(result_lines) + "\n")

    section_display = target.capitalize()
    return f"Added to **{section_display}**: {fact_line}"


def list_facts() -> str:
    """List all facts currently in user.md."""
    if not USER_MD.exists():
        return "Error: user.md not found."

    text = USER_MD.read_text()
    sections = get_sections(text)
    lines = text.splitlines()

    output = ["**User Memory**\n"]
    for section_name in KNOWN_SECTIONS:
        if section_name not in sections:
            continue
        start, end = sections[section_name]
        header = lines[start]
        facts = []
        for i in range(start + 1, end):
            line = lines[i].strip()
            if line.startswith("- ") and "(to be filled in)" not in line and "(Add important" not in line:
                facts.append(line)

        output.append(f"**{section_name.capitalize()}**")
        if facts:
            for f in facts:
                output.append(f"  {f}")
        else:
            output.append("  (empty)")
        output.append("")

    return "\n".join(output)


def cmd_add(args: argparse.Namespace) -> None:
    """Handle the add command."""
    result = add_fact(args.fact, section=args.section)
    print(result)


def cmd_list(args: argparse.Namespace) -> None:
    """Handle the list command."""
    print(list_facts())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage Merlin's user memory (memory/user.md).",
        epilog="""
Examples:
  # Add a fact to the Notes section (default)
  uv run remember.py add "Prefers dark mode in all editors"

  # Add to a specific section
  uv run remember.py add "Name: Alex" --section identity
  uv run remember.py add "Likes concise responses" --section preferences
  uv run remember.py add "Working on Merlin bot project" --section context

  # List all stored facts
  uv run remember.py list

Sections:
  identity     — Name, timezone, personal details
  preferences  — Communication style, tool preferences
  context      — Current projects, interests, ongoing work
  notes        — General facts (default)

For longer knowledge entries, use kb_add.py instead.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    add_parser = subparsers.add_parser(
        "add",
        help="Add a fact to user memory",
        description="Add a fact to user.md. Appends to the specified section.",
    )
    add_parser.add_argument("fact", help="The fact to remember")
    add_parser.add_argument("--section", "-s",
                            choices=KNOWN_SECTIONS,
                            help="Section to add to (default: notes)")
    add_parser.set_defaults(func=cmd_add)

    list_parser = subparsers.add_parser(
        "list",
        help="List all stored facts",
        description="Show all facts currently in user.md.",
    )
    list_parser.set_defaults(func=cmd_list)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
