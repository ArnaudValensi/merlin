"""
Skill registry — engine-agnostic discovery and aggregation of SKILL.md skills.

Merlin owns one canonical skill registry; per-engine adapters surface it in
each engine's native format (Claude Code plugin, ~/.agents/skills symlinks,
system-prompt fallback). Sources, in precedence order:

1. The core repo ``skills/`` directory (shipped operational skills, always
   active regardless of the bot)
2. Built-in extensions' ``skills/`` directories (e.g. ``merlin-bot/skills/``),
   gated by the extension's enabled state
3. Installed extensions' ``~/.merlin/extensions/<ext>/skills/``
4. The user-skill home ``<merlin-home>/skills-user/`` (personal skills,
   always active, per-environment / unsynced)

On a name conflict the first source wins (core > extension > user), so a core
skill can never be shadowed: a user or extension skill that collides with a
core skill's name is dropped with a warning. This is deliberate — core skills
are founder-authored and trusted, so nothing may silently override them.

Aggregation: every registered skill directory is symlinked into
``~/.merlin/skills/`` (the canonical dir), rebuilt at startup so disabled
extensions' skills disappear. Only symlinks are managed; a real directory a
user placed there is left alone with a warning.

Governance: every discovered skill is enabled (v1, no per-skill toggles).
The Extensions page lists each extension's skills and commands read-only.
"""

from __future__ import annotations

import logging
import os
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
    """Extract frontmatter fields from a SKILL.md as strings.

    Delegates to the canonical parser in lib/frontmatter.py (shared with
    the notes module); list values (YAML arrays) are joined for the
    string-only registry fields. Returns {} when the file is missing,
    unreadable, or has no frontmatter.
    """
    from lib.frontmatter import parse_frontmatter

    try:
        text = skill_md.read_text(errors="replace")
    except OSError:
        return {}

    meta, _body = parse_frontmatter(text)
    return {
        key: ", ".join(value) if isinstance(value, list) else str(value)
        for key, value in meta.items()
    }


def user_skills_dir() -> Path:
    """The user-skill home: personal skills, always active, per-environment.

    Personal skills are behavior, not notes data, so they live in a dedicated
    ``skills-user/`` home rather than under the notes dir. Per-environment and
    unsynced by decision (no cross-environment sync system yet).
    """
    return paths.merlin_home() / "skills-user"


def core_skills_dir() -> Path:
    """The core repo skill source: shipped operational skills, always active."""
    return paths.app_dir() / "skills"


def canonical_dir() -> Path:
    """The aggregated canonical skill directory."""
    return paths.merlin_home() / "skills"


def list_source_skills(source_id: str, skills_dir: Path) -> list[SkillSpec]:
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
    """Build {name -> SkillSpec} from the always-active core repo ``skills/``
    source, the extension dirs, and the user-skill home.

    ``extension_dirs`` maps extension id -> extension root (its ``skills/``
    subdirectory is the source). The core source is prepended and the user
    home appended unconditionally. Iteration order sets precedence; on a name
    conflict the first wins with a warning (core > extension > user), so a
    core skill is never shadowed.
    """
    registry: dict[str, SkillSpec] = {}

    sources: list[tuple[str, Path]] = [("core", core_skills_dir())]
    sources += [
        (ext_id, ext_dir / "skills") for ext_id, ext_dir in extension_dirs.items()
    ]
    sources.append(("user", user_skills_dir()))

    for source_id, skills_dir in sources:
        for spec in list_source_skills(source_id, skills_dir):
            existing = registry.get(spec.name)
            if existing is not None:
                if existing.source == "core":
                    # A lower-precedence source tried to reuse a core skill's
                    # name. Core cannot be shadowed; surface it as a security
                    # event, not a neutral conflict.
                    logger.warning(
                        "Blocked skill override: '%s' from %s ignored - a core "
                        "skill of that name takes precedence and cannot be "
                        "shadowed",
                        spec.name,
                        spec.source,
                    )
                else:
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


def list_canonical_skills() -> list[SkillSpec]:
    """Read skills back from the canonical dir (works across processes).

    The in-memory registry only exists in the process that built it; cron
    runner subprocesses and the engine fallback read the aggregation instead.
    """
    return list_source_skills("canonical", canonical_dir())


# ---------------------------------------------------------------------------
# Interactive shims and per-skill engine link farms
# ---------------------------------------------------------------------------


def agents_skills_dir() -> Path:
    """The cross-engine user skill location (~/.agents/skills).

    Read natively by OpenCode and Pi. Skills must sit directly at
    ``~/.agents/skills/<name>/SKILL.md`` (no nesting), hence per-skill links.
    """
    return Path.home() / ".agents" / "skills"


def claude_skills_dir() -> Path:
    """Claude Code's personal skill location (~/.claude/skills)."""
    return Path.home() / ".claude" / "skills"


def _is_merlin_link(entry: Path) -> bool:
    """True if entry is a symlink we own (raw target inside the canonical dir)."""
    if not entry.is_symlink():
        return False
    try:
        raw_target = Path(os.readlink(entry))
    except OSError:
        return False
    canonical = canonical_dir()
    return raw_target == canonical / entry.name or raw_target.is_relative_to(canonical)


def sync_shim_links(target_dir: Path) -> None:
    """Mirror the canonical skills into ``target_dir`` via per-skill symlinks.

    Unlike the canonical dir (where every symlink is managed), shim dirs are
    shared with the user's own skills: only symlinks pointing into the
    canonical dir are touched. Foreign entries are skipped with a warning on
    name collision, never overwritten.
    """
    canonical = canonical_dir()
    names = {spec.name for spec in list_canonical_skills()}

    target_dir.mkdir(parents=True, exist_ok=True)

    for entry in target_dir.iterdir():
        if _is_merlin_link(entry) and entry.name not in names:
            entry.unlink()

    for name in sorted(names):
        link = target_dir / name
        if _is_merlin_link(link):
            continue
        if link.exists() or link.is_symlink():
            logger.warning(
                "Skill shim '%s' skipped: %s already exists and is not "
                "managed by Merlin",
                name,
                link,
            )
            continue
        link.symlink_to(canonical / name)


def sync_interactive_shims() -> None:
    """Refresh the user-scope shims (~/.claude/skills and ~/.agents/skills).

    This is what gives the user's own terminal agents Merlin's skills from
    any cwd. Called at server startup and by 'merlin setup'.
    """
    for target in (claude_skills_dir(), agents_skills_dir()):
        try:
            sync_shim_links(target)
        except OSError as e:
            logger.warning("Could not sync skill shims into %s: %s", target, e)
