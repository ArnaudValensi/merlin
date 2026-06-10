# Your Agent

Merlin does not ship its own AI. You bring your agent CLI (Claude Code by
default, OpenCode too) and Merlin gives it a body: open the
[web terminal](terminal.md) from anywhere, phone included, run your agent,
and it already knows Merlin. Its skills are in place, the `merlin` CLI is
its toolbox, and the [notes system](notes.md) is its memory. This is the
"your own agent" node of the flywheel: what you teach your agent in the
terminal lands in the shared notes and knowledge base, and the
[Discord bot](bot.md) and [cron jobs](cron.md) run the same agent with the
same brain doc, so every channel gets sharper together.

## Run your agent anywhere

Open the terminal (or any shell on the machine) and launch `claude` or
`opencode`. No per-project setup: Merlin mirrors its canonical skill set
(`~/.merlin/skills`) as symlinks into `~/.claude/skills` (read by Claude
Code) and `~/.agents/skills` (the location OpenCode and Pi read), so your
agent finds Merlin's skills from any working directory. The mirror is
refreshed automatically at every server startup; `merlin setup` refreshes
it on demand.

## Hand your agent the brain doc

`merlin agent` prints the agent operating doc: what Merlin is and how to
drive it. Pipe it into a prompt, or let the agent run it when it needs a
refresher. Optional layers stack on top:

```bash
merlin agent                        # the brain doc
merlin agent --personality          # plus your personality file
merlin agent --personality --user   # plus your user memory
```

Each layer comes from the module that composes Merlin's managed
channels, so a layer printed here is byte-identical to that layer as the
bot or cron injects it (the channels pick different layer sets: cron
skips personality by design, and the bot adds a Discord style overlay). `merlin agent --help` has the
details.

What the doc teaches, in short: every capability is a `merlin` subcommand
that works from any directory, with `merlin --help` as the catalog and
`merlin config` to resolve data paths instead of hardcoding them; the
three-layer notes system (user memory via `merlin remember`, daily logs,
the Zettelkasten knowledge base) is the shared memory across channels and
anything worth keeping must land there, because scheduled runs start
fresh; `merlin cron` manages scheduled jobs; `merlin skills` lists skills
and where personal ones live; the extension authoring guide adds new
capabilities; and edits to repo-tracked notes get committed and pushed.

## See what your agent knows

```bash
merlin skills
```

Lists every skill and its source. Precedence is core, then extension,
then user: shadowed skills and skills from disabled extensions show up
dimmed. Personal skills live in your skills-user directory (resolve it
with `merlin config skills-user-dir`).

## Pick the engine for managed channels

The Discord bot and cron jobs invoke your agent through Merlin's engine
abstraction. Set `AGENT_ENGINE` in the environment to choose:
`claude-code` (default) or `opencode`. This only affects Merlin's managed
channels; in the terminal you run whatever CLI you launch yourself.

## Teach it new tricks

Any capability you add as an extension becomes available to every
channel: executable files under `~/.merlin/extensions/<id>/commands/`
appear as `merlin` subcommands with no restart, and `SKILL.md` folders
under `skills/` teach the agent when to use them. The full walkthrough is
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
