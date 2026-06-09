"""Permanent markdown link checker.

Walks every .md file in the repo, extracts relative inline links, and
asserts each target exists. Protects the documentation against file moves
(added by the agent-documentation epic's docs restructuring).

External links (http/https/mailto) and pure anchors are skipped. Fenced
code blocks and inline code spans are ignored: example links there are
illustrative, not navigable.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "#")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".pytest_cache"}


def _strip_fenced_code(text: str) -> str:
    out, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def _md_files() -> list[Path]:
    return [
        path
        for path in sorted(REPO_ROOT.rglob("*.md"))
        if not SKIP_DIRS.intersection(path.parts)
    ]


def test_repo_has_markdown_files():
    """Guard: the walker must find the docs (catches a broken REPO_ROOT)."""
    files = _md_files()
    assert any(f.name == "MERLIN.md" for f in files)
    assert len(files) > 10


def test_relative_markdown_links_resolve():
    broken: list[str] = []
    for md in _md_files():
        text = _strip_fenced_code(md.read_text(encoding="utf-8", errors="replace"))
        text = INLINE_CODE_RE.sub("", text)
        for match in LINK_RE.finditer(text):
            target = match.group(1)
            if target.startswith(SKIP_PREFIXES):
                continue
            rel = target.split("#")[0]
            if not rel:
                continue
            if not (md.parent / rel).exists():
                broken.append(f"{md.relative_to(REPO_ROOT)}: broken link -> {target}")
    assert not broken, "Broken markdown links:\n" + "\n".join(broken)
