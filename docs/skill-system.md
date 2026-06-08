# Skill System

Reference for Merlin's skill registry: how SKILL.md skills are discovered,
aggregated, surfaced to each engine, and listed. Skills are progressive-
disclosure capabilities (a `SKILL.md` with `name` + `description` frontmatter
and a body the agent reads on demand).

> Code: `lib/skills.py` (registry + aggregation + shims), `cli.py:run_skills`
> (`merlin skills`), `ext_commands.py` (extension source resolution),
> `lib/engines/*` (per-engine adapters), `main.py:_rebuild_skill_registry`
> (startup).

## Design

Merlin owns **one canonical skill registry** and delegates exposure: per-engine
adapters surface it in each engine's native format. The registry is built from
filesystem sources alone (no manifest, no server required for the CLI).

## Sources and precedence

Sources, highest precedence first:

| # | Source | Path | Active when |
|---|--------|------|-------------|
| 1 | **Core** | `app_dir()/skills/` (repo `skills/`) | Always — independent of the bot |
| 2 | **Built-in extensions** | e.g. `merlin-bot/skills/` | The extension is enabled |
| 3 | **Installed extensions** | `~/.merlin/extensions/<ext>/skills/` | The extension is enabled |
| 4 | **User home** | `~/.merlin/skills-user/` | Always (per-environment, unsynced) |

On a name conflict the **first source wins (core > extension > user)**, so a
**core skill can never be shadowed**: a user or extension skill that collides
with a core skill's name is dropped and logged as a security event —
`Blocked skill override: '<name>' from <source> ignored ...` (console +
`~/.merlin/logs/merlin.log`). Core skills are founder-authored and trusted, so
nothing may silently override them. Core may shadow an extension; extensions
take precedence over the user home.

Why core is its own source: managed environments run with the bot off, and the
bot used to be the only built-in skill source — so its skills never aggregated
and `~/.claude/skills` came up empty. The always-active core `skills/` source
fixes that. `discord` stays under `merlin-bot/skills/` because it is genuinely
bot-gated.

The user home is `~/.merlin/skills-user/` (not the notes dir): personal skills
are agent behavior, not notes data. It is per-environment and not synced;
cross-environment sync is a deliberately deferred reflection.

## Registry build and canonical aggregation

`build_registry(extension_dirs)` prepends the core source, appends the user
home, and resolves winners (see `audit_sources`, the shared enumerator that
also powers `merlin skills`). Each winning skill directory is symlinked into
the **canonical dir** `~/.merlin/skills/<name>`, rebuilt at server startup and
by `merlin setup` so a disabled extension's skills disappear. Only symlinks are
managed; a real directory a user placed there is left alone with a warning.

`list_canonical_skills()` reads the canonical dir back (works across
processes — cron runner subprocesses never see the server's in-memory
registry).

## Engine adapters

The canonical dir is surfaced in each engine's native format:

| Engine | Mechanism |
|--------|-----------|
| Claude Code | Generated plugin at `~/.merlin/skills-plugin/` (`.claude-plugin/plugin.json` + a `skills/` symlink to the canonical dir), passed via `--plugin-dir` on every invocation (`lib/engines/claude_code.py:ensure_skills_plugin`) |
| OpenCode | Per-skill symlinks `~/.agents/skills/<name>` -> canonical (`lib/engines/opencode.py`) |
| Other / unknown | Base-class fallback: a table of skill names + descriptions injected into the system prompt, pointing at each `SKILL.md` path |

Engine surfaces regenerate at invocation time (idempotent cheap checks) in
addition to the startup rebuild.

## Interactive shims (the user's own agents)

At startup and `merlin setup`, the canonical skills are also symlinked into the
user's personal scopes so their own terminal agents get Merlin skills from any
cwd:

- `~/.claude/skills/<name>` (Claude Code)
- `~/.agents/skills/<name>` (engines that read it natively)

Shims never overwrite a path Merlin does not own: only symlinks pointing into
the Merlin home are managed; foreign entries are skipped with a warning.

## `merlin skills`

Lists every skill grouped by source in precedence order — the global view the
per-extension Extensions-page audit lacks. It reuses `audit_sources` (the same
enumerator `build_registry` routes through), so the listing cannot drift from
the live registry. Shadowed skills (blocked by core, or shadowed by a
higher-precedence source) and skills from disabled extensions are shown dimmed;
a staleness hint prints when the live folder differs from what would aggregate
now. Full descriptions are kept (wrapped on a TTY, one line when piped).

## Authoring a skill

Create `<source>/skills/<name>/SKILL.md` (or `~/.merlin/skills-user/<name>/`
for a personal skill) with frontmatter:

```markdown
---
name: my-skill
description: One line the agent uses to decide when to invoke this skill.
---

Body the agent reads on demand.
```

`name` defaults to the directory name if omitted. Restart Merlin (or run
`merlin setup`) to re-aggregate. Verify with `merlin skills`.

## Governance

v1: every discovered skill is enabled (no per-skill toggles). `merlin skills`
is read-only. The Extensions page lists each extension's skills and commands
read-only as a security surface; core and user skills do not belong to an
extension, so they appear in `merlin skills` rather than that per-extension
audit.

## Key code

| File | Responsibility |
|------|----------------|
| `lib/skills.py` | `build_registry`, `audit_sources`, canonical aggregation (`rebuild`), interactive shims (`sync_interactive_shims`), source paths (`core_skills_dir`, `user_skills_dir`, `canonical_dir`) |
| `cli.py` | `run_skills` (`merlin skills`), `skills-user-dir` config key |
| `ext_commands.py` | `all_extension_states` / `enabled_extension_source_dirs` (which extensions contribute, and their enabled flag) |
| `lib/engines/claude_code.py` | Claude Code plugin adapter (`--plugin-dir`) |
| `lib/engines/opencode.py` | OpenCode `~/.agents/skills` adapter |
| `main.py` | `_skill_source_dirs`, `_rebuild_skill_registry` at server startup |
