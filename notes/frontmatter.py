"""YAML frontmatter parser for markdown files."""

import re

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FIELD_RE = re.compile(r"^(\w+):\s*(.+)$", re.MULTILINE)
ARRAY_RE = re.compile(r"\[([^\]]*)\]")


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown content.

    Returns (metadata_dict, body_without_frontmatter).
    If no frontmatter found, returns ({}, full_content).
    """
    match = FRONTMATTER_RE.match(content)
    if not match:
        return {}, content

    raw = match.group(1)
    body = content[match.end():]
    meta = {}

    for field_match in FIELD_RE.finditer(raw):
        key = field_match.group(1)
        value = field_match.group(2).strip()

        # Parse YAML arrays: [a, b, c]
        arr_match = ARRAY_RE.match(value)
        if arr_match:
            items = [item.strip().strip("'\"") for item in arr_match.group(1).split(",")]
            meta[key] = [i for i in items if i]
        else:
            meta[key] = value.strip("'\"")

    return meta, body
