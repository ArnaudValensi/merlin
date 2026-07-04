# Development Setup

How to run, change, and validate Merlin from a git checkout. This is the
contributor entry point: set up here, then read
[architecture.md](architecture.md) for how the systems fit, and the
per-system docs in this folder when you touch one.

## Requirements

Python and [uv](https://docs.astral.sh/uv/); `fd` is a hard requirement
(the boot error tells you how to install it). Optional: tmux (the web
terminal needs it). Missing optional dependencies degrade gracefully at
boot: a warning, a grayed-out nav item, never a crash.

## Run from a checkout

```bash
git clone https://github.com/ArnaudValensi/merlin.git
cd merlin
uv run main.py
```

Dev mode is detected from the `.git/` directory: the app runs from the
repo, user data still lives under `~/.merlin/`. The dev-vs-installed
split is documented in [standalone-cli.md](standalone-cli.md).

Variants:

```bash
uv run main.py                   # dashboard
./restart.sh                     # restart everything in background (single process)
```

The dashboard listens on http://localhost:3123.

## Discord bot from a checkout

Bot creation (token, channel ID) is covered in [the bot doc](../bot.md);
for a checkout the token lives in a local env file:

```bash
cp merlin-bot/.env.example merlin-bot/.env
# edit .env with your bot token and channel ID
```

## Validate

```bash
uv run scripts.py validate    # full: lint + format + typecheck + tests
uv run scripts.py lint        # ruff + pyright only
uv run scripts.py test        # unit + integration tests (~4s)
uv run scripts.py test-e2e    # E2E with Playwright (~2min)
```

E2E tests need Playwright; first-time setup:

```bash
uv run --with playwright playwright install firefox
```

Validation includes the doc link-checker test, so run `validate` after
documentation changes too. The doc-writing contract is
[documentation-principles.md](documentation-principles.md).

## UI changes

Read [dashboard-architecture.md](dashboard-architecture.md) before
touching templates or CSS (theme variables, JS patterns, how to add
pages), and verify visually with the screenshot skill:

```bash
uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --user admin --pass <pass>
```
