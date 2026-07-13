# The merlin CLI

`merlin` is the power-user surface: everything you can reach in the
dashboard (and a few things you can't) is operable from a shell, from any
directory. The dashboard is the front door; the CLI is how you and
[your agent](agents.md) drive the same system from scripts, jobs, and
terminal sessions.

`merlin --help` is the authoritative catalog, and every subcommand
documents itself with `merlin <command> --help`. Syntax and flags live
there, not here: this page is the map.

## The families

- `merlin` / `merlin start`: the dashboard server. Bare `merlin` runs the
  setup wizard on first start, then starts everything (dashboard, jobs,
  extensions, the bot if configured). [Getting started](getting-started.md)
  covers the first run.
- `merlin agent`: print the agent brain doc, the operating manual
  [your agent](agents.md) reads; flags compose the persona layers.
- `merlin job`: manage [jobs](jobs.md) from the shell: agent or command
  runs fired by a schedule, a webhook, or by hand.
- `merlin notes` / `merlin kb` / `merlin remember`: search and feed the
  [knowledge base](notes.md).
- `merlin chat`: send, reply, and react on the chat channel the
  [Discord bot](bot.md) serves.
- `merlin skills`: list every agent skill and where it comes from (core,
  an extension, or your personal skills), in precedence order; see
  [Extensions](extensions.md).
- `merlin dashboard-url`: print the dashboard address (credentials
  embedded when a password is set), for scripts and agents.
- `merlin setup` / `merlin update` / `merlin config` / `merlin version`:
  install and configuration. [Getting started](getting-started.md) walks
  through all four.
- `merlin-clip`: the clipboard bridge between your shell and the browser
  clipboard; covered in [Terminal](terminal.md).

Extensions add their own commands: an extension that ships a `commands/`
folder appears automatically as `merlin <extension> <command>`. The
[Extensions](extensions.md) page audits what each one adds;
[Creating extensions](creating-extensions.md) shows how to ship your own.
