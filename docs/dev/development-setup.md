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
uv run cli.py restart            # restart the running server (stop + fresh start)
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

## Throwaway instance for UI work

Screenshotting a UI state you do not currently have (agent-state pills in
every state, a populated Sessions panel) means fabricating that state.
Never fabricate it in your own environment: build a disposable instance
with its own home, port, and tmux server.

```bash
TMPHOME=/tmp/merlin-sandbox TT=/tmp/merlin-sandbox-tmux
rm -rf "$TMPHOME" "$TT"; mkdir -p "$TMPHOME" "$TT"
printf 'DASHBOARD_USER=admin\nDASHBOARD_PASS=sandbox\n' > "$TMPHOME/config.env"

env -u MERLIN_SAAS_TOKEN -u MERLIN_ENVIRONMENT_SLUG -u TMUX \
  MERLIN_HOME="$TMPHOME" TMUX_TMPDIR="$TT" MERLIN_DEV=1 \
  nohup uv run main.py --port 3199 > "$TMPHOME/server.log" 2>&1 &
echo "PID=$!"    # capture it, you need it to stop the instance
```

Why each part:

- **`-u MERLIN_SAAS_TOKEN`**: the SaaS tunnel starts whenever the token is
  set, so a naive run connects your test instance to the real portal and
  flips auth into SaaS mode. Without the token, local mode requires
  `config.env` to exist, hence writing one.
- **A separate port**: 3123 is the live instance. Do not disturb it.
- **`TMUX_TMPDIR` plus `-u TMUX`**: the web terminal spawns a bare
  `tmux new-session -A` (see [web-terminal.md](web-terminal.md)), so there
  is no flag to redirect it. `TMUX_TMPDIR` is the only lever that gives
  the instance its own tmux server, and it only works with `TMUX` unset,
  since an inherited `$TMUX` outranks it. Keep the path short: unix
  sockets cap around 108 characters.

### Tearing it down

Two cleanup traps have each cost a working session. Both look like the
sandbox misbehaving; both are self-inflicted.

- **Do not `pkill -f "port 3199"`**, or any pattern that appears in your
  own command line. `pkill -f` matches the shell running your command and
  kills it mid-way. Use the PID you captured at launch.
- **Do not aim `kill-server` with an environment variable.** Target the
  socket, which nothing can redirect:

  ```bash
  tmux -S "$TT/tmux-$(id -u)/default" kill-server
  ```

  The tempting `TMUX_TMPDIR=$TT tmux kill-server` does the opposite of
  what it reads like. With no `-S` or `-L`, tmux resolves its target from
  `$TMUX`, which is set whenever you are working inside tmux, so it
  ignores `TMUX_TMPDIR` and kills your real server: every window, every
  agent session in it. Skipping the kill is not the fix either, since
  `rm -rf` of the tmpdir strands the server with no socket, reachable
  only by PID.
