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
it from your phone, updating, and rolling back, is in
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
- [Contributor docs](docs/dev/architecture.md): architecture and internals, for changing Merlin's code (index in [`CLAUDE.md`](CLAUDE.md))

## The merlin CLI

`merlin --help` is the full capability catalog; every subcommand documents
itself with `merlin <command> --help`. The families:

- `merlin start`: the dashboard server (bare `merlin` does the same)
- `merlin agent`: print the agent brain doc (what agents operating Merlin read)
- `merlin cron`: scheduled agent runs
- `merlin notes` / `merlin kb` / `merlin remember`: the knowledge base
- `merlin chat`: send messages to the chat channel
- `merlin skills`: list agent skills and their sources
- `merlin dashboard-url`: print the dashboard URL
- `merlin setup` / `merlin update` / `merlin config` / `merlin version`: install and config

## Development Setup

For working on Merlin from a git checkout:

```bash
git clone https://github.com/ArnaudValensi/merlin.git
cd merlin
uv run main.py --no-tunnel
```

### Running

```bash
uv run main.py                   # Dashboard only
uv run main.py --no-tunnel       # Dashboard without tunnel
./restart.sh                     # Dashboard + bot (background)
```

### Discord Bot (running from a checkout)

Bot-creation steps are in [the bot doc](docs/bot.md), then:

```bash
cp merlin-bot/.env.example merlin-bot/.env
# Edit .env with your bot token and channel ID
```

### Tests

```bash
uv run scripts.py validate    # Full validation: lint + format + typecheck + tests
uv run scripts.py lint         # Lint only (ruff + pyright)
uv run scripts.py test         # Unit + integration tests (~4s)
uv run scripts.py test-e2e     # E2E with Playwright (~2min)
```

E2E tests require Playwright. First-time setup: `uv run --with playwright playwright install firefox`
