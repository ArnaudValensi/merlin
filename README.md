# Merlin

Portable mobile dev environment — file browser, terminal, git viewer, notes editor, accessible from anywhere via Cloudflare tunnel.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/ArnaudValensi/merlin/master/install.sh | bash
```

This installs to `~/.merlin/`, adds `merlin` to your PATH, and prompts for optional dependencies (tmux, cloudflared).

Then run setup:

```bash
merlin setup    # Configure password, tunnel, Discord bot token
merlin          # Start the dashboard
```

Open **http://localhost:3123** to access the dashboard.

### Update

```bash
merlin update
```

### Rollback

```bash
ln -sfn ~/.merlin/versions/0.1.0 ~/.merlin/current
```

Old versions are kept in `~/.merlin/versions/` and never auto-deleted.

## Development Setup

For working on Merlin from a git checkout:

```bash
git clone https://github.com/ArnaudValensi/merlin.git
cd merlin
uv run main.py --no-tunnel
```

### Discord Bot (Optional)

1. Create a Discord bot at the [Developer Portal](https://discord.com/developers/applications)
2. Enable **Message Content Intent** under Privileged Gateway Intents
3. Invite with `bot` scope + `Send Messages`, `Add Reactions` permissions

```bash
cp merlin-bot/.env.example merlin-bot/.env
# Edit .env with your bot token and channel ID
```

### Running

```bash
uv run main.py                   # Dashboard only
uv run main.py --no-tunnel       # Dashboard without tunnel
./restart.sh                     # Dashboard + bot (background)
```

### CLI Commands

```bash
merlin                           # Start dashboard (default)
merlin start --port 8080         # Custom port
merlin start --dev               # Dev mode (resolve paths from repo)
merlin version                   # Print version
merlin setup                     # Interactive config wizard
merlin update                    # Update to latest release
```

### Voice Transcription

Merlin supports voice input in the terminal UI and Discord voice messages. Audio is transcribed using one of three backends (in priority order):

| Backend | Setup | Speed | Requirements |
|---------|-------|-------|-------------|
| **SaaS proxy** (Groq Whisper) | Merlin Cloud account | ~1s | None (portal handles it) |
| **OpenAI Whisper API** | Set `OPENAI_API_KEY` | ~1s | API key (~$0.006/min) |
| **Local (faster-whisper)** | None (default) | 2-5s | ~1.5GB model download, ffmpeg |

In SaaS mode, transcription is proxied through the portal (no API key needed). For self-hosted setups:

```bash
merlin setup                    # Prompts for API key
# or manually:
echo "OPENAI_API_KEY=sk-your-key" >> ~/.merlin/config.env
```

Voice input features: upload progress indicator, server-side text injection (resilient to phone disconnection after upload), and auto-enter toggle for hands-free command submission.

### Tests

```bash
uv run scripts.py validate    # Full validation: lint + format + typecheck + tests
uv run scripts.py lint         # Lint only (ruff + pyright)
uv run scripts.py test         # Unit + integration tests (~4s)
uv run scripts.py test-e2e     # E2E with Playwright (~2min)
```

E2E tests require Playwright. First-time setup: `uv run --with playwright playwright install firefox`

## Dashboard Pages

**Core** (always available):
- **Files** — Browse the filesystem
- **Terminal** — Web terminal (xterm.js + tmux)
- **Commits** — Git commit browser
- **Notes** — Markdown notes editor

**Bot** (when merlin-bot is present):
- **Overview** — Bot status, invocations, errors
- **Performance** — Execution time charts
- **Logs** — Tabbed view with filters

## Troubleshooting

**Bot not responding:**
- Check `logs/merlin.log` for errors
- Verify `DISCORD_CHANNEL_IDS` in `.env` matches your channel
- Ensure Message Content Intent is enabled in Discord Developer Portal

**Cron jobs not running:**
- Check if `merlin_bot.py` is running (scheduler runs inside the bot)
- Check dashboard for crash alerts

**Discord send failing:**
- Verify token in `.env`
- Check bot has permissions in the channel
