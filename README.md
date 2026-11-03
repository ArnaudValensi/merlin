<!-- Source: readme/banner.html; capture #banner transparent at 2x after any brand change. -->
![Merlin](readme/banner.png)

**Your agent operating environment**

The place where your AI agents work. Merlin turns any Linux box or Mac
into that place: a real shell, real files, real git, in your browser, for
the agents you already use (Claude Code, Codex: anything that runs in a
terminal). You run it on one machine and control it from any other, phone
included. Open source and self-hosted first;
[Merlin Cloud](https://merlincloud.dev) is the hosted version.

## The two pillars

Merlin doesn't ship its own AI, and it puts nothing between you and your
agent. It gives your agents a place to operate: a machine, and a scheduler.

- **A coding environment**: a terminal with your agents in it, the
  filesystem, and the git history, all in the browser, built for the way
  you work now: the agent types, you direct, review, and decide from
  whatever screen is in your hand. Every agent window shows its state on
  the Sessions board, and Merlin notifies you, phone included, when an
  agent finishes or asks something while you are not looking. Talk instead
  of typing: voice input with built-in transcription works in the terminal.
- **Agent orchestration**: jobs that run without you, fired by a
  schedule or a webhook. A job is a script or an agent; every run opens
  its own tmux window with the agent's real interface, so you can watch
  it, step in, answer it, and Merlin tells you how it ended.

**Extensions and skills** bolt new pages, commands, and know-how onto
both. Notes, a knowledge base you and your agents both maintain, ships as
one of them, off until you switch it on.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/ArnaudValensi/merlin/master/install.sh | bash
merlin setup    # password and optional voice transcription
merlin          # start the dashboard
```

Open **http://localhost:3123**. The full walkthrough, including reaching
it from your phone (bring your own tunnel, or let
[Merlin Cloud](docs/getting-started.md#merlin-cloud) handle remote
access), updating, and rolling back, is in
[Getting started](docs/getting-started.md).

## Documentation

Start here, then one doc per piece of Merlin:

- [Getting started](docs/getting-started.md): install, setup, first run, phone access, update and rollback
- [Terminal](docs/terminal.md): the web terminal, touch gestures, mobile toolbar, clipboard, voice input
- [Your agent](docs/agents.md): running Claude Code or Codex with Merlin's skills, windows and notifications
- [Files](docs/files.md): file browser, code viewer, image and 3D model preview
- [Commits](docs/commits.md): git history and diffs, review your agent's work from your phone
- [Jobs](docs/jobs.md): agent or command runs on a schedule, a webhook, or on demand, each in its own tmux window; notifications and history
- [Notifications](docs/notifications.md): how Merlin tells you an agent finished or asks, in the page and on your phone
- [Notes](docs/notes.md): the optional notes extension, a knowledge base your agent keeps (user facts, daily logs, knowledge base)
- [Extensions](docs/extensions.md): enable, configure, and audit what your agent can do
- [Creating extensions](docs/creating-extensions.md): add your own dashboard pages, `merlin` commands, and agent skills
- [The merlin CLI](docs/cli.md): drive everything from the shell; the command map, with syntax in `merlin --help`
- [Contributor docs](docs/dev/development-setup.md): working on Merlin's code; setup and tests first, then [architecture](docs/dev/architecture.md) and the per-system internals
