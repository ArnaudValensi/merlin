"""
Skill registry — engine-agnostic discovery and aggregation of SKILL.md skills.

Merlin owns one canonical skill registry; per-engine adapters surface it in
each engine's native format (Claude Code plugin, ~/.agents/skills symlinks,
system-prompt fallback). Sources, in precedence order:

1. Built-in extensions' ``skills/`` directories (e.g. ``merlin-bot/skills/``)
2. Installed extensions' ``~/.merlin/extensions/<ext>/skills/``
3. The user-skill home ``<notes-dir>/skills/`` (personal skills as data;
   follows the user through notes sync)

Aggregation: every registered skill directory is symlinked into
``~/.merlin/skills/`` (the canonical dir), rebuilt at startup so disabled
extensions' skills disappear. Only symlinks are managed; a real directory a
user placed there is left alone with a warning.

Governance: every discovered skill is enabled (v1, no per-skill toggles).
The Extensions page lists each extension's skills and commands read-only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import paths

logger = logging.getLogger("merlin.skills")

# Last built registry — read by the Extensions page audit section.
_registry: dict[str, "SkillSpec"] = {}


@dataclass(frozen=True)
class SkillSpec:
    """One discovered skill."""

    name: str
    description: str
    path: Path  # The skill directory (contains SKILL.md)
    source: str  # Extension id, or "user" for the user-skill home


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def parse_skill_frontmatter(skill_md: Path) -> dict[str, str]:
    """Extract flat ``key: value`` frontmatter fields from a SKILL.md.

    Minimal parser (same convention as the notes tooling): no nested YAML.
    Returns {} when the file is missing, unreadable, or has no frontmatter.
    """
    try:
        text = skill_md.read_text(errors="replace")
    except OSError:
        return {}

    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}

    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def user_skills_dir() -> Path:
    """The user-skill home: personal skills as data, under the notes dir."""
    return paths.notes_dir() / "skills"


def canonical_dir() -> Path:
    """The aggregated canonical skill directory."""
    return paths.merlin_home() / "skills"


def _discover_source(source_id: str, skills_dir: Path) -> list[SkillSpec]:
    """Discover skills in one source's skills/ directory."""
    if not skills_dir.is_dir():
        return []

    specs: list[SkillSpec] = []
    for entry in sorted(skills_dir.iterdir()):
        if entry.name.startswith((".", "_")) or not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        fields = parse_skill_frontmatter(skill_md)
        name = fields.get("name") or entry.name
        description = fields.get("description", "")
        specs.append(
            SkillSpec(
                name=name,
                description=description,
                path=entry.resolve(),
                source=source_id,
            )
        )
    return specs


def build_registry(extension_dirs: dict[str, Path]) -> dict[str, SkillSpec]:
    """Build {name -> SkillSpec} from extension dirs plus the user-skill home.

    ``extension_dirs`` maps extension id -> extension root (its ``skills/``
    subdirectory is the source). Iteration order sets precedence; on a name
    conflict the first wins with a warning.
    """
    registry: dict[str, SkillSpec] = {}

    sources: list[tuple[str, Path]] = [
        (ext_id, ext_dir / "skills") for ext_id, ext_dir in extension_dirs.items()
    ]
    sources.append(("user", user_skills_dir()))

    for source_id, skills_dir in sources:
        for spec in _discover_source(source_id, skills_dir):
            existing = registry.get(spec.name)
            if existing is not None:
                logger.warning(
                    "Skill name conflict: '%s' from %s shadowed by %s",
                    spec.name,
                    spec.source,
                    existing.source,
                )
                continue
            registry[spec.name] = spec

    return registry


# ---------------------------------------------------------------------------
# Canonical aggregation
# ---------------------------------------------------------------------------


def _sync_skill_links(target_dir: Path, registry: dict[str, SkillSpec]) -> None:
    """Make ``target_dir`` contain exactly one symlink per registered skill.

    Only symlinks are managed: stale ones are removed, missing ones created.
    Real files/directories are preserved (with a warning on name collision).
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    for entry in target_dir.iterdir():
        if not entry.is_symlink():
            logger.warning(
                "Unmanaged entry in %s: %s (left alone)", target_dir, entry.name
            )
            continue
        spec = registry.get(entry.name)
        if spec is None or entry.resolve() != spec.path:
            entry.unlink()

    for name, spec in registry.items():
        link = target_dir / name
        if link.is_symlink():
            continue  # Already correct (stale ones were removed above)
        if link.exists():
            logger.warning(
                "Cannot link skill '%s' into %s: name taken by an unmanaged entry",
                name,
                target_dir,
            )
            continue
        link.symlink_to(spec.path)


def rebuild(extension_dirs: dict[str, Path]) -> dict[str, SkillSpec]:
    """Build the registry and rebuild the canonical aggregation dir.

    Called at server startup (and by ``merlin setup``). Returns the registry
    and caches it for the Extensions page.
    """
    global _registry
    registry = build_registry(extension_dirs)
    _sync_skill_links(canonical_dir(), registry)
    _registry = registry
    logger.info(
        "Skill registry built: %d skill(s) from %s",
        len(registry),
        ", ".join(sorted({s.source for s in registry.values()})) or "(no sources)",
    )
    return registry


def get_registry() -> dict[str, SkillSpec]:
    """The registry from the last rebuild (empty if never built)."""
    return _registry
