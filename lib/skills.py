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

import contextlib
import copy
import json
import logging
import os
import tempfile
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
    source: str  # "core", an extension id, or "user" for the user-skill home


@dataclass(frozen=True)
class AuditedSkill:
    """One skill entry for the full audit view (every source, winners + losers).

    Unlike the registry (winners only), the audit keeps every discovered skill
    so callers can show what was shadowed or came from a disabled source.
    """

    name: str
    description: str
    path: Path
    source: str  # "core", an extension id, or "user"
    source_active: bool  # False for e.g. a disabled extension; never wins
    shadowed_by: str | None  # winning source if this entry lost a name clash


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


def audit_sources(sources: list[tuple[str, Path, bool]]) -> list[AuditedSkill]:
    """Enumerate every skill from every source, in precedence order, tagging
    each entry with its status.

    ``sources`` is ``(source_id, skills_dir, active)`` in precedence order.
    ``active=False`` marks a source (e.g. a disabled extension) whose skills
    are surfaced but never win. A skill wins its name when it is the first
    *active* source to claim it; a later claimant carries ``shadowed_by`` set
    to the winner's source. Inactive sources never participate in winner
    resolution (``shadowed_by`` stays ``None``; ``source_active`` is ``False``).

    This is the single enumerator shared with ``build_registry``, so the live
    registry and any audit view cannot drift.
    """
    winners: dict[str, str] = {}
    audited: list[AuditedSkill] = []
    for source_id, skills_dir, active in sources:
        for spec in list_source_skills(source_id, skills_dir):
            shadowed_by: str | None = None
            if active:
                winner = winners.get(spec.name)
                if winner is not None:
                    shadowed_by = winner
                else:
                    winners[spec.name] = source_id
            audited.append(
                AuditedSkill(
                    name=spec.name,
                    description=spec.description,
                    path=spec.path,
                    source=source_id,
                    source_active=active,
                    shadowed_by=shadowed_by,
                )
            )
    return audited


def build_registry(extension_dirs: dict[str, Path]) -> dict[str, SkillSpec]:
    """Build {name -> SkillSpec} from the always-active core repo ``skills/``
    source, the extension dirs, and the user-skill home.

    ``extension_dirs`` maps extension id -> extension root (its ``skills/``
    subdirectory is the source). The core source is prepended and the user
    home appended unconditionally. Iteration order sets precedence; on a name
    conflict the first wins with a warning (core > extension > user), so a
    core skill is never shadowed.
    """
    sources: list[tuple[str, Path, bool]] = [("core", core_skills_dir(), True)]
    sources += [
        (ext_id, ext_dir / "skills", True) for ext_id, ext_dir in extension_dirs.items()
    ]
    sources.append(("user", user_skills_dir(), True))

    registry: dict[str, SkillSpec] = {}
    for entry in audit_sources(sources):
        if entry.shadowed_by is not None:
            if entry.shadowed_by == "core":
                # A lower-precedence source tried to reuse a core skill's name.
                # Core cannot be shadowed; surface it as a security event, not
                # a neutral conflict.
                logger.warning(
                    "Blocked skill override: '%s' from %s ignored - a core "
                    "skill of that name takes precedence and cannot be "
                    "shadowed",
                    entry.name,
                    entry.source,
                )
            else:
                logger.warning(
                    "Skill name conflict: '%s' from %s shadowed by %s",
                    entry.name,
                    entry.source,
                    entry.shadowed_by,
                )
            continue
        registry[entry.name] = SkillSpec(
            name=entry.name,
            description=entry.description,
            path=entry.path,
            source=entry.source,
        )

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

    The in-memory registry only exists in the process that built it; job
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


# ---------------------------------------------------------------------------
# Agent-state pill hooks: consent config
#
# The tmux window pills (terminal/tmux.conf) need a Claude Code state hook
# installed into the user's ~/.claude/settings.json. That edits the user's own
# config, so it is consent-gated by a single config value:
#
#   auto  sync silently on every start ("always, don't ask")
#   ask   (default) surface the consent prompt in the dashboard on drift
#   off   remove Merlin's entries, never sync, never prompt
#
# This is the single source of truth read by the CLI, the Settings page, the
# reconciler, and the consent banner.
# ---------------------------------------------------------------------------

AGENT_STATE_HOOKS_KEY = "AGENT_STATE_HOOKS"
AGENT_STATE_HOOKS_MODES = ("auto", "ask", "off")
AGENT_STATE_HOOKS_DEFAULT = "ask"


def _config_env_read(key: str) -> str | None:
    """Read a single key from config.env, falling back to os.environ.

    Mirrors paths.load_config_env's parsing (optional ``export `` prefix,
    matching surrounding quotes stripped) but for one key, without mutating
    the process environment.
    """
    path = paths.config_path()
    if path.exists():
        try:
            lines = path.read_text().splitlines()
        except OSError:
            lines = []
        for line in lines:
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export ") :]
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                return v
    return os.environ.get(key)


def _config_env_write(key: str, value: str) -> None:
    """Set a single key in config.env, preserving all other lines (0600)."""
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    replaced = False
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
                lines.append(f"{key}={value}")
                replaced = True
            else:
                lines.append(line)
    if not replaced:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def agent_state_hooks_mode() -> str:
    """The current consent mode (auto|ask|off). Unknown/unset -> default ask."""
    val = (_config_env_read(AGENT_STATE_HOOKS_KEY) or "").strip().lower()
    return val if val in AGENT_STATE_HOOKS_MODES else AGENT_STATE_HOOKS_DEFAULT


def set_agent_state_hooks_mode(mode: str) -> str:
    """Validate and persist the consent mode. Returns the normalized value.

    Raises ValueError on an invalid mode so callers surface a clear error.
    """
    normalized = (mode or "").strip().lower()
    if normalized not in AGENT_STATE_HOOKS_MODES:
        raise ValueError(
            f"invalid agent-state-hooks mode {mode!r}; "
            f"expected one of {', '.join(AGENT_STATE_HOOKS_MODES)}"
        )
    _config_env_write(AGENT_STATE_HOOKS_KEY, normalized)
    return normalized


# ---------------------------------------------------------------------------
# Agent-state pill hooks: the settings.json reconciler
#
# The interactive `claude` command line is the user's, not Merlin's, so the
# state hook has to be pre-registered where Claude Code reads it on every
# launch: the `hooks` key of ~/.claude/settings.json (--plugin-dir is CLI-only,
# so it cannot help the interactive path). This reconciler owns exactly three
# entries there and nothing else, the same idempotent, drift-only shape as
# sync_interactive_shims(): it re-writes them whenever what Merlin ships drifts
# from what is installed, so an 'auto' user never re-runs setup after an update.
#
# Merlin's entries are MARKED (a sentinel + version baked into the command
# string, which is a harmless trailing shell comment) so they can be found,
# compared, refreshed, and removed. Foreign entries are never touched, and the
# write is a collision-safe atomic merge: read, merge in memory, write a temp
# file, parse-verify, atomic rename.
# ---------------------------------------------------------------------------

_HOOK_MARKER = "merlin:agent-state-pill"
_HOOK_VERSION = 2  # v2: SessionStart also runs agent-session-init.sh (board sid/cwd)
# Claude Code event -> agent-state.sh argument.
_HOOK_EVENTS = {
    "UserPromptSubmit": "busy",  # you submitted a prompt: the agent is working
    "Stop": "done",  # the agent finished a turn: waiting on you
    "SessionStart": "idle",  # a session started / resumed: nothing running yet
}


def claude_settings_path() -> Path:
    """Claude Code's user settings file (~/.claude/settings.json)."""
    return claude_skills_dir().parent / "settings.json"


def _hook_command(state: str) -> str:
    """The shipped command for a state, with the version marker as a trailing
    shell comment (ignored on execution, used for identification + drift)."""
    script = paths.app_dir() / "terminal" / "hooks" / "agent-state.sh"
    return f'bash "{script}" {state}  # {_HOOK_MARKER}:v{_HOOK_VERSION}'


def _session_init_command() -> str:
    """The SessionStart companion that mints the board's stable session id and
    pins the launch cwd (see terminal/hooks/agent-session-init.sh). Carries the
    same marker so it is found, refreshed, and removed with the state hooks."""
    script = paths.app_dir() / "terminal" / "hooks" / "agent-session-init.sh"
    return f'bash "{script}"  # {_HOOK_MARKER}:v{_HOOK_VERSION}'


def _is_merlin_group(group: object) -> bool:
    """True if a hook matcher-group is one Merlin owns (marked command)."""
    if not isinstance(group, dict):
        return False
    entries = group.get("hooks")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if isinstance(entry, dict) and _HOOK_MARKER in str(entry.get("command", "")):
            return True
    return False


def _merlin_group(event: str, state: str) -> dict:
    """The hook entries Merlin owns for one event. SessionStart carries a second
    command (the board session-init) in the same group."""
    entries = [{"type": "command", "command": _hook_command(state)}]
    if event == "SessionStart":
        entries.append({"type": "command", "command": _session_init_command()})
    return {"hooks": entries}


def _read_claude_settings() -> dict | None:
    """Read settings.json. {} if absent, None if unreadable/invalid or not an
    object (caller must then NOT write, to never clobber the user's file)."""
    path = claude_settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _hooks_shape_ok(settings: dict) -> bool:
    """The `hooks` block, if present, must be the object-of-lists shape we can
    safely merge into. Anything else -> bail rather than risk corrupting it."""
    hooks = settings.get("hooks")
    if hooks is None:
        return True
    if not isinstance(hooks, dict):
        return False
    for event in _HOOK_EVENTS:
        value = hooks.get(event)
        if value is not None and not isinstance(value, list):
            return False
    return True


def _reconcile_install(settings: dict) -> dict:
    """Desired settings with Merlin's three groups fresh: strip any existing
    Merlin groups (stale version / old path) and append the shipped ones.
    Foreign groups keep their place and order."""
    out = copy.deepcopy(settings)
    hooks = out.setdefault("hooks", {})
    for event, state in _HOOK_EVENTS.items():
        groups = [g for g in hooks.get(event, []) if not _is_merlin_group(g)]
        groups.append(_merlin_group(event, state))
        hooks[event] = groups
    return out


def _reconcile_remove(settings: dict) -> dict:
    """Desired settings with every Merlin group removed; foreign groups kept.
    Event keys and the `hooks` block are dropped only if they end up empty."""
    out = copy.deepcopy(settings)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event in list(hooks.keys()):
        value = hooks.get(event)
        if not isinstance(value, list):
            continue
        kept = [g for g in value if not _is_merlin_group(g)]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        out.pop("hooks", None)
    return out


def _write_claude_settings(data: dict) -> None:
    """Collision-safe atomic write: render, parse-verify, temp file, rename.
    Preserves the file's existing permission bits when it already exists."""
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) + "\n"
    json.loads(text)  # parse-verify before touching the real file
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def install_interactive_hooks() -> bool:
    """Install/refresh Merlin's state hooks. Returns True if it wrote a change.
    No-op (and no write) when already in sync or when the file can't be merged
    safely."""
    settings = _read_claude_settings()
    if settings is None or not _hooks_shape_ok(settings):
        logger.warning(
            "Skipping agent-state hook install: %s is unreadable or has an "
            "unexpected shape",
            claude_settings_path(),
        )
        return False
    desired = _reconcile_install(settings)
    if desired == settings:
        return False
    _write_claude_settings(desired)
    return True


def remove_interactive_hooks() -> bool:
    """Remove Merlin's state hooks, leaving foreign entries untouched. Returns
    True if it wrote a change."""
    settings = _read_claude_settings()
    if settings is None:
        return False
    desired = _reconcile_remove(settings)
    if desired == settings:
        return False
    _write_claude_settings(desired)
    return True


def interactive_hooks_drift() -> bool:
    """True if installing would change settings.json (not installed, or a
    version/path update made the shipped command differ). False when the file
    can't be merged safely (we won't nag about something we won't touch)."""
    settings = _read_claude_settings()
    if settings is None or not _hooks_shape_ok(settings):
        return False
    return _reconcile_install(settings) != settings


def sync_interactive_hooks() -> str:
    """Reconcile the state hooks per the consent mode. Called at every startup
    and after a consent change. Never raises into the caller's happy path.

    Returns a short status: 'synced' | 'in-sync' | 'removed' | 'clean' |
    'pending' (ask-mode drift the dashboard banner should surface).
    """
    mode = agent_state_hooks_mode()
    if mode == "off":
        return "removed" if remove_interactive_hooks() else "clean"
    if mode == "auto":
        return "synced" if install_interactive_hooks() else "in-sync"
    # ask: startup never writes; the dashboard consent banner drives any sync.
    return "pending" if interactive_hooks_drift() else "in-sync"
