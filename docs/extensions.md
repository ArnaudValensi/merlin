# Extensions

The Extensions page (last item in the sidebar, `/extensions`) is where you
see everything plugged into Merlin: switch extensions on or off, fill in
their configuration, and audit exactly what skills and commands each one
ships before trusting it. Extensions come in three tiers, shown as card
groups: **Core** (Files, Terminal, Commits: always on, a lock icon
instead of a toggle), **Built-in** (Notes on by default, Merlin Bot off by
default), and **Installed** (anything in `~/.merlin/extensions/`, on by
default; the group appears once something is installed).

![Extensions page on mobile](creating-extensions/extensions-page-mobile.png)

In the flywheel, this page is the trust-and-growth gate. Extensions are how
your agent gains new capabilities: commands callable from every surface
([terminal](terminal.md), [Discord bot](bot.md), [cron](cron.md)) and
skills that teach it when to use them. The audit view is where you check
what code your agent is about to gain before you flip the switch.

## See what is installed

Open `/extensions`. Each card shows the extension's icon, name, and
description. A card whose extension failed to load shows the error message
in red.

## Enable or disable an extension

Flip the toggle on the card. The state persists in
`~/.merlin/extensions.json`, and a banner appears: "Changes require a
restart to take effect", because extensions load and unload only at
startup. Core extensions show a lock instead of a toggle: they cannot be
turned off.

What disabling actually removes, after restart: the extension's pages, nav
items, and skills. Its CLI commands still run (`merlin <ext> <cmd>` never
checks the toggle); delete the directory if you want the commands gone too.

## Restart from the page

Click **Restart** in the banner. The button restarts Merlin; the page
waits for the server to come back (up to ~10 seconds) and reloads itself.

## Configure an extension

Cards for extensions that take configuration show a **Configure** link
that expands an inline form. Secret fields render as password inputs and
show `••••••••` once set; "Requires configuration" appears while a
required field is empty. Save writes to `~/.merlin/config.env` (mode
0600): only keys the extension declares are accepted, and clearing a field
deletes the key. Saving also triggers the restart banner.

## Audit before trusting

Every card that ships skills or commands has a read-only **Skills &
commands** link. It expands to list each skill with its description and
each command with its one-line help, extracted from the code without
executing it. The list ends with the warning that matters: skills and
commands run with Merlin's permissions. Read it before enabling anything
you did not write.

## Install an extension

Drop a directory (or have your agent create one) at
`~/.merlin/extensions/<id>/`. Its CLI commands work immediately: the CLI
rescans the directory on every invocation, no restart needed. Skills and
web pages appear after a restart (or `merlin setup` for skills alone). To
build your own, see [Creating extensions](creating-extensions.md).

## Uninstall an extension

Delete its directory under `~/.merlin/extensions/` and restart. Discovery
is just a directory scan, so removal is the whole operation.

## List commands and skills from the CLI

`merlin --help` prints "Built-in extensions" and "Installed extensions"
sections with every extension command and its one-line help. Running
`merlin <ext>` with no subcommand lists that extension's commands.
`merlin skills` lists every skill grouped by source in precedence order
(core > extension > user), dims skills from disabled extensions, and tells
you when the live skill folder is out of date.

## Mobile notes

- The sidebar is an overlay drawer on phones: tap the hamburger in the top
  bar. Extensions sits last in the drawer, with enlarged touch targets.
- On narrow screens each card's toggle (or lock) drops to its own
  full-width row below the card text.
- The restart banner stacks vertically with a full-width Restart button.
- The red error dot on the Extensions nav icon shows on mobile too.

## Troubleshooting

- **An extension failed to load.** Its card shows the error in red with
  the toggle disabled, and a red dot appears on the Extensions icon in the
  sidebar. Fix the cause and restart.
- **"Extension name X is reserved".** Installed extensions cannot reuse
  the name of a core command, a built-in extension, or an alias (`start`,
  `setup`, `notes`, `cron`, `kb`, ...). The error shows up as an errored
  card, at the CLI, and in `merlin --help`. Fix: rename the directory.
- **`merlin <ext> <cmd>` exits 126: file not executable.** Run the
  `chmod +x` command the error prints. Files starting with `.` or `_` are
  never commands.
- **Toggled or installed something but the dashboard did not change.**
  Pages, nav, and skills only change at startup: use the Restart button or
  restart Merlin. `merlin skills` says explicitly when the live skill
  folder is stale; `merlin setup` refreshes skills without a full restart.
- **Restart looks stuck.** The page waits up to ~10 seconds for the server
  to come back, then reloads anyway. If it errors after that, Merlin did
  not come back up: check it from the terminal.
- **Disabled an extension but its commands still work.** By design:
  disabling removes skills and pages, not CLI dispatch. Delete the
  directory to remove the commands too.
- **A config value silently did not save.** Only fields the extension
  declares are written, and saving an empty value deletes the key from
  `~/.merlin/config.env`.
