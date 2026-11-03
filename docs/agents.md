# Your Agent

Merlin does not ship its own AI. You bring your agent CLI (Claude Code or
Codex) and Merlin gives it a place to work: open the
[web terminal](terminal.md) from anywhere, phone included, run your agent,
and it already knows Merlin. Its skills are in place and the `merlin` CLI
is its toolbox. Merlin puts nothing between you and the agent: no wrapper,
no injected persona, no conversation of its own. One agent, two places to
operate: the windows you open in the terminal, and the windows
[jobs](jobs.md) open for it when you are not there.

## Run your agent anywhere

Open the terminal (or any shell on the machine) and launch `claude` or
`codex`. No per-project setup: Merlin mirrors its canonical skill set
(`~/.merlin/skills`) as symlinks into `~/.claude/skills` (read by Claude
Code), `~/.agents/skills` and `~/.codex/skills` (read by Codex and other
agent CLIs), so your agent finds Merlin's skills from any working
directory. The mirror is refreshed automatically at every server startup;
`merlin setup` refreshes it on demand.

## Windows, states and notifications

Every agent runs in a tmux window. With your consent (the Settings page,
or `merlin config agent-state-hooks auto`), Merlin installs a small hook
in your agent's own settings that stamps the window's state: idle, busy,
waiting for an answer, done. The Sessions panel of the terminal shows
every window with that state, and [notifications](notifications.md) tell
you, on your phone too, when an agent finishes or asks something in a
window you are not looking at. The same hook is how a job run's finish is
known, so jobs need it installed.

A window survives closing the tab or losing signal: reopen the page and
you are exactly where you left off. After a reboot, the
[workspace restore](dev/workspace-restore.md) brings every window back on
the same conversation.

## Hand your agent the brain doc

`merlin agent` prints the agent operating doc: what Merlin is and how to
drive it. Pipe it into a prompt, or let the agent run it when it needs a
refresher. Nothing injects it: the agent you launch is the agent as it
is, and the doc is one read away.

What the doc teaches, in short: every capability is a `merlin` subcommand
that works from any directory, with `merlin --help` as the catalog and
`merlin config` to resolve data paths instead of hardcoding them; how
windows, states and notifications work; what a job run is (the `[Job:
<id>]` prompt line, every permission check bypassed, the deadline) and
how to behave in one; `merlin skills` lists skills and where personal ones
live; the extension authoring guide adds new capabilities; and edits to
repo-tracked files get committed and pushed.

## See what your agent knows

```bash
merlin skills
```

Lists every skill and its source. Precedence is core, then extension,
then user: shadowed skills and skills from disabled extensions show up
dimmed. Personal skills live in your skills-user directory (resolve it
with `merlin config skills-user-dir`).

## Give it a memory (optional)

The [notes extension](notes.md), off until you enable it on the
Extensions page, gives your agent a knowledge base, daily logs and user
memory under `merlin notes`, `merlin kb` and `merlin remember`, with the
`merlin-notes` skill that teaches it the method. Without it, Merlin keeps
no memory of its own and does not expect the agent to keep one: what must
outlive a window goes in files.

## Teach it new tricks

Any capability you add as an extension becomes available in every window:
executable files under `~/.merlin/extensions/<id>/commands/` appear as
`merlin` subcommands with no restart, and `SKILL.md` folders under
`skills/` teach the agent when to use them. The full walkthrough is
[creating extensions](creating-extensions.md); the brain doc points
agents at the same guide, so your agent can author extensions for itself.

## Mobile notes

The web terminal attaches to a persistent tmux session, so an agent run
survives closing the tab or losing signal on the phone: reopen the page
and you are exactly where you left off. The touch toolbar, voice input,
and phone clipboard are covered in the [terminal doc](terminal.md).

## Troubleshooting

**A new extension's skills do not show up for your own agent.** The shims
refresh at server startup, not the moment an extension is enabled.
Restart Merlin, or rerun `merlin setup` to completion (declining its
overwrite prompt skips the refresh). `merlin skills` detects this drift
itself and prints a note telling you to do exactly that.

**You already have a skill with the same name in `~/.claude/skills` or
`~/.agents/skills`.** Merlin never overwrites your files: it only manages
symlinks pointing into `~/.merlin/skills`, skips the colliding shim, and
warns. Rename or remove your entry if you want Merlin's skill instead.

**`merlin agent` says the brain doc is not found.** The doc is read from
the Merlin install; `merlin update` restores it.

**`merlin setup` warns it could not refresh skill shims.** The refresh is
best-effort and setup still succeeds; the server repeats the sync at
every startup, so a restart usually clears it.

**No pills, no notifications, jobs stuck "starting".** The agent-state
hook is not installed: check the consent on the Settings page or run
`merlin config agent-state-hooks auto`. Codex runs its hooks only in a
directory it trusts; see [Jobs](jobs.md#the-state-hooks-must-be-installed).
