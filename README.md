# Merlin

A personal AI assistant that runs as one process on your machine and
compounds over time. Web terminal, files, git, and notes in a dashboard you
reach from anywhere (phone included) via Cloudflare tunnel.

## How the pieces fit

Merlin is not a terminal plus a chat bot plus a scheduler. It is one
assistant with shared memory:

- You code in the **web terminal** from anywhere. Phone included; coding
  from everywhere is why Merlin exists.
- Work and conversations land in the **notes / knowledge base**
  (Zettelkasten-style, plain markdown). That is what the assistant
  remembers.
- The **Discord bot** is the same assistant on another channel: it reads
  and writes that knowledge base and acts for you.
- **Cron** runs agents on a schedule that feed the knowledge base and
  report back.
- **Extensions and skills** bolt new commands and know-how onto the loop.

Each piece feeds the others. The longer it runs, the more it knows.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/ArnaudValensi/merlin/master/install.sh | bash
```

This installs to `~/.merlin/`, adds `merlin` to your PATH, and prompts for
optional dependencies (tmux, cloudflared).

Then run setup and start:

```bash
merlin setup    # Configure password, tunnel, and the optional Discord bot
merlin          # Start the dashboard
```

With the tunnel enabled, the startup log prints your public https URL
(`merlin dashboard-url` prints it any time). Open it on your phone, log in
with the password you set, and you are coding from anywhere. Without a
tunnel, open **http://localhost:3123**.

### Update

```bash
merlin update
```

### Rollback

```bash
ln -sfn ~/.merlin/versions/0.1.0 ~/.merlin/current
```

Old versions are kept in `~/.merlin/versions/` and never auto-deleted.

## Dashboard Pages

**Core** (always available):
- **Files**: browse the filesystem
- **Terminal**: web terminal (xterm.js + tmux), the front door
- **Commits**: git commit browser
- **Notes**: markdown editor over Merlin's memory (user facts, daily logs,
  knowledge base)
- **Cron**: schedule recurring agent jobs

**Bot** (when the Discord bot extension is enabled):
- **Overview**: bot status, invocations, errors
- **Performance**: execution time charts
- **Logs**: tabbed view with filters

## Voice Input

Merlin supports voice input in the terminal UI and Discord voice messages.
Audio is transcribed using one of three backends (in priority order):

| Backend | Setup | Speed | Requirements |
|---------|-------|-------|-------------|
| **SaaS proxy** (Groq Whisper) | Merlin Cloud account | ~1s | None (portal handles it) |
| **OpenAI Whisper API** | Set `OPENAI_API_KEY` | ~1s | API key (~$0.006/min) |
| **Local (faster-whisper)** | None (default) | 2-5s | ~1.5GB model download, ffmpeg |

Merlin runs self-hosted; there is also a hosted offering, Merlin Cloud
(merlincloud.dev), where transcription is handled for you. For self-hosted
setups:

```bash
merlin setup                    # Prompts for API key
# or manually:
echo "OPENAI_API_KEY=sk-your-key" >> ~/.merlin/config.env
```

Voice input features: upload progress indicator, server-side text injection
(resilient to phone disconnection after upload), and auto-enter toggle for
hands-free command submission.

## Discord assistant (optional)

The Discord bot is your assistant's chat channel: message it from anywhere,
each conversation gets a thread, and it shares the same notes and knowledge
base as everything else. To set it up:

1. Create a Discord bot at the [Developer Portal](https://discord.com/developers/applications)
2. Enable **Message Content Intent** under Privileged Gateway Intents
3. Invite it with the `bot` scope + `Send Messages`, `Add Reactions` permissions
4. Run `merlin setup` and paste the bot token (you can rerun setup any time
   to add the bot later)
5. Allow your channel: `echo 'DISCORD_CHANNEL_IDS=<channel-id>' >> ~/.merlin/config.env`

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

## Documentation

- [Web terminal](docs/web-terminal.md): terminal usage, mobile toolbar, voice input, clipboard
- [Creating extensions](docs/creating-extensions.md): add your own `merlin` commands and agent skills
- [Contributor docs](docs/dev/architecture.md): architecture and internals, for changing Merlin's code (index in [`CLAUDE.md`](CLAUDE.md))

For the agent, notes/KB, and cron, the CLI is the documentation:
`merlin agent` prints the agent's operating doc; `merlin cron --help`,
`merlin notes --help`, and `merlin skills` cover the rest.

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

Same bot-creation steps as above, then:

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

## Troubleshooting

**Bot not responding:**
- Check `logs/merlin.log` for errors
- Verify `DISCORD_CHANNEL_IDS` in `.env` matches your channel
- Ensure Message Content Intent is enabled in Discord Developer Portal

**Cron jobs not running:**
- Check that `merlin` is running (the scheduler lives in the main process,
  no bot required)
- Check the `/cron` dashboard page and `merlin cron history` for failures

**Discord send failing:**
- Verify token in `.env`
- Check bot has permissions in the channel
