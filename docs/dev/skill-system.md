# Skill System

Reference for Merlin's skill registry: how SKILL.md skills are discovered,
aggregated, mirrored to the user's agents, and listed. Skills are progressive-
disclosure capabilities (a `SKILL.md` with `name` + `description` frontmatter
and a body the agent reads on demand).

> Code: `lib/skills.py` (registry + aggregation + shims), `cli.py:run_skills`
> (`merlin skills`), `ext_commands.py` (extension source resolution),
> `main.py:_rebuild_skill_registry` (startup).

## Design

Merlin owns **one canonical skill registry** and mirrors it into the places
the user's agents read skills from. The registry is built from filesystem
sources alone (no manifest, no server required for the CLI).

## Sources and precedence

Sources, highest precedence first:

| # | Source | Path | Active when |
|---|--------|------|-------------|
| 1 | **Core** | `app_dir()/skills/` (repo `skills/`) | Always |
| 2 | **Built-in extensions** | e.g. `notes/skills/`, `app/skills/` | The extension is enabled (and, for a flagged one, its flag is on) |
| 3 | **Installed extensions** | `~/.merlin/extensions/<ext>/skills/` | The extension is enabled |
| 4 | **User home** | `~/.merlin/skills-user/` | Always (per-environment, unsynced) |

On a name conflict the **first source wins (core > extension > user)**, so a
**core skill can never be shadowed**: a user or extension skill that collides
with a core skill's name is dropped and logged as a security event —
`Blocked skill override: '<name>' from <source> ignored ...` (console +
`~/.merlin/logs/merlin.log`). Core skills are founder-authored and trusted, so
nothing may silently override them. Core may shadow an extension; extensions
take precedence over the user home.

Why core is its own source: the operational skills (`merlin`, `merlin-jobs`,
`merlin-dashboard`, `merlin-review`) must aggregate whatever extensions are
on, so they ship in the always-active core `skills/`. A skill that belongs to
an extension ships with it and follows its enabled state: `merlin-notes`
lives under `notes/skills/` and exists only when notes is enabled.

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
processes — job runner subprocesses never see the server's in-memory
registry).

## The shims (the user's own agents)

At startup and `merlin setup`, the canonical skills are also symlinked into the
user's personal scopes so their own terminal agents get Merlin skills from any
cwd:

- `~/.claude/skills/<name>` (Claude Code)
- `~/.agents/skills/<name>` (agents that read it natively)
- `$CODEX_HOME/skills/<name>` (Codex, default `~/.codex/skills`; written unconditionally, harmless if Codex isn't installed)

Shims never overwrite a path Merlin does not own: only symlinks pointing into
the Merlin home are managed; foreign entries are skipped with a warning.

## Interactive hooks (agent-state pills)

`sync_interactive_hooks()` is the shims' sibling for the tmux agent-state pills
(the `terminal/` window pills), and for the job runs, whose finish is known
through the same hooks. The agent's command line is the user's, not Merlin's
(a job run launches the agent's real interface too), so the state hook has to
live in the `hooks` key of `~/.claude/settings.json` and `~/.codex/hooks.json`,
which the agents read on every launch. The reconcilers own their marked groups
there (`UserPromptSubmit` -> busy, `Stop` -> done, `SessionStart` -> idle, the
permission prompts -> ask, the voice context), marked by a sentinel + version
baked into the command string (a harmless trailing shell comment).

Like the shims it is idempotent and drift-only, so an update re-writes the hook
without the user re-running setup. Unlike the shims it is **consent-gated** (it
edits the user's own config): the `agent-state-hooks` config value drives it —
`auto` installs/updates silently, `off` removes Merlin's entries, `ask`
(default) only detects drift and leaves the dashboard consent banner to ask.
The write is a collision-safe atomic merge (read, merge in memory, temp file,
parse-verify, rename); foreign hooks are never touched. Called from
`_rebuild_skill_registry()` at startup, from the Settings save, and from the
consent banner (`/api/agent-state-hooks`). See the epic in `merlin-saas`
(`epics/cli/session-status-signals/`).

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
| `lib/skills.py` | `build_registry`, `audit_sources`, canonical aggregation (`rebuild`), interactive shims (`sync_interactive_shims`), agent-state hook reconciler (`sync_interactive_hooks`, `agent_state_hooks_mode`), source paths (`core_skills_dir`, `user_skills_dir`, `canonical_dir`) |
| `cli.py` | `run_skills` (`merlin skills`), `skills-user-dir` config key |
| `ext_commands.py` | `all_extension_states` / `enabled_extension_source_dirs` (which extensions contribute, and their enabled flag) |
| `main.py` | `_skill_source_dirs`, `_rebuild_skill_registry` at server startup |
