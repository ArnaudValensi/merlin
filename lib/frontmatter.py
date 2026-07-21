"""YAML frontmatter parser for markdown files.

Canonical parser for in-process contexts (notes routes, lib/skills, job
runner), backed by PyYAML. The standalone command scripts under
notes/commands/ keep private copies: they run in isolated PEP 723
environments and declare pyyaml themselves.
"""

import datetime
import re

import yaml

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _normalize(value):
    """Keep frontmatter values JSON-friendly: dates become ISO strings."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown content.

    Returns (metadata_dict, body_without_frontmatter). Date values are
    normalized to ISO strings. If no frontmatter is found or it fails to
    parse, returns ({}, full_content).
    """
    match = FRONTMATTER_RE.match(content)
    if not match:
        return {}, content

    body = content[match.end() :]
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, content
    if not isinstance(meta, dict):
        return {}, content
    return {k: _normalize(v) for k, v in meta.items()}, body
