# Merlin

A suite of dev tools built around your favorite AI agent (Claude Code,
OpenCode, pluggable engines). One process on your machine: web terminal,
files, git, and notes in a dashboard you can reach from anywhere, phone
included, plus chat and scheduled channels that run your agent with shared
memory. Talk instead of typing: voice input with built-in transcription
works in the terminal and the chat.

## How the pieces fit

Merlin doesn't ship its own AI; it gives your agent a body: tools,
channels, and one shared memory.

- You code in the **web terminal** from anywhere. Phone included; coding
  from everywhere is why Merlin exists.
- Work and conversations land in the **notes / knowledge base**
  (Zettelkasten-style, plain markdown). That is what your agent remembers.
- The **Discord bot** puts the same agent in your chat: it reads and
  writes that knowledge base and acts for you.
- **Cron** runs the agent on a schedule to feed the knowledge base and
  report back.
- **Extensions and skills** bolt new commands and know-how onto the loop.

Each piece feeds the others. The longer it runs, the more your agent
knows.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/ArnaudValensi/merlin/master/install.sh | bash
merlin setup    # password, optional Discord bot and voice transcription
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
- [Files](docs/files.md): file browser, code viewer, image and 3D model preview
- [Commits](docs/commits.md): git history and diffs, review your agent's work from your phone
- [Notes](docs/notes.md): the notes editor and your agent's memory (user facts, daily logs, knowledge base)
- [Cron](docs/cron.md): scheduled agent runs, reports, history and performance
- [Discord bot](docs/bot.md): the same agent in your chat, threads, voice messages
- [Extensions](docs/extensions.md): enable, configure, and audit what your agent can do
- [Your agent](docs/agents.md): running Claude Code or OpenCode with Merlin's skills and memory
- [Creating extensions](docs/creating-extensions.md): add your own dashboard pages, `merlin` commands, and agent skills
- [The merlin CLI](docs/cli.md): drive everything from the shell; the command map, with syntax in `merlin --help`
- [Contributor docs](docs/dev/development-setup.md): working on Merlin's code; setup and tests first, then [architecture](docs/dev/architecture.md) and the per-system internals
