# Getting Started

Merlin runs as one process on your machine: a web dashboard on port 3123 with a web terminal, files, commits, notes, and cron, behind a password login. This page takes you from a fresh machine to that dashboard in your pocket. It is the entry node of the flywheel: the one process you start here hosts every other piece, and the `~/.merlin` home the installer creates is the shared memory they all read and feed.

![Merlin running on a phone](terminal/phone-merlin.jpg)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/ArnaudValensi/merlin/master/install.sh | bash
```

The installer needs `uv` (it offers to install it if missing) and prompts for two optional dependencies: tmux (needed for the web terminal) and cloudflared (only needed for the deprecated bundled tunnel). It downloads the latest release into `~/.merlin/versions/<tag>/`, points the `~/.merlin/current` symlink at it, and offers to add `~/.merlin/current/bin` to your PATH in your shell rc.

When it finishes: run `merlin` to start (you may need to restart your shell first).

## Run the setup wizard

Bare `merlin` runs the wizard automatically on first start when no config exists yet. You can also run it any time:

```bash
merlin setup
```

It asks, in order:

- **Dashboard password**: empty means no auth, fine for local-only use.
- **Enable Cloudflare tunnel?**: the bundled tunnel is deprecated; say no unless you already rely on it (see "Reach it from your phone" below).
- **Discord bot token**: Enter to skip; you can add it later (see [bot.md](bot.md)).
- **OpenAI API key for voice transcription**: Enter to skip. Without it, transcription falls back to a local model (~1.5GB download, works offline); with it, the Whisper API costs about $0.006/min.

Answers land in `~/.merlin/config.env` (mode 0600, the only config file). Re-running setup shows your current values as defaults and asks before overwriting. The wizard also aggregates your agent skills so they are ready before the first run.

## Start and log in

```bash
merlin
```

Bare `merlin` is `merlin start`. It prints `Merlin starting on http://0.0.0.0:3123` plus the working directory it serves. Open **http://localhost:3123**: you get a single password field, and a successful login sets a signed cookie that lasts 30 days and survives restarts. Log out at `/logout`. Port, host, and the rest are in `merlin start --help`.

That one process is the whole system: the cron scheduler starts with it, extensions load, and the skill registry your agent reads is rebuilt. The terminal, the bot, and cron all share the notes under `~/.merlin`, which is why the longer it runs, the more your agent knows.

## Reach it from your phone

Expose the dashboard with your own tunnel or reverse proxy (Tailscale, Cloudflare, nginx, whatever you trust), or use Merlin Cloud (next section), which handles remote access for you.

The bundled cloudflared tunnel still works today but is deprecated and will be removed. Enabled with no tunnel token, it starts a Quick Tunnel and prints `Quick Tunnel active: https://<random>.trycloudflare.com` at startup; that URL changes on every restart.

`merlin dashboard-url` prints your dashboard address with credentials embedded when a password is set. It resolves `MERLIN_DASHBOARD_URL` first, then the configured tunnel hostname, then `http://localhost:3123`. If you front Merlin with your own proxy, set `MERLIN_DASHBOARD_URL` in `config.env` so the command (and your agent) know the stable address.

## Merlin Cloud

[Merlin Cloud](https://merlincloud.dev) is the hosted service that pairs with Merlin. Connect your self-hosted instance with a token and it handles the remote-access plumbing: a tunnel to a stable HTTPS URL for your dashboard, plus a voice transcription backend, with nothing else to configure:

```bash
merlin --saas-token YOUR_TOKEN    # saved to config.env for future runs
```

Merlin Cloud can also host the whole thing: a managed environment where Merlin and your agent run on cloud hardware, reachable from any device, with no server of your own. Self-hosting stays a first-class path; everything in these docs applies to both.

## Update

```bash
merlin update
```

It downloads the new release into `~/.merlin/versions/<tag>/`, atomically swaps the `current` symlink, prints `Updated: old -> new` and a ready-to-paste revert command. Merlin also checks for updates at startup (at most once per 24h) and asks `Update now? [y/N]`; on yes it updates and re-execs into the new version.

## Roll back

```bash
ln -sfn ~/.merlin/versions/<old-version> ~/.merlin/current
```

Old versions are kept in `~/.merlin/versions/` and never auto-deleted, so a bad update is one symlink away from fixed. `merlin update` prints this exact command after every update.

## Check version, config, and what landed on disk

`merlin version` prints the active version. `merlin config` lists every resolved path (notes dir, logs, cron jobs, sessions, ...); `merlin config <key>` prints one value for use in shell commands.

Inside `~/.merlin`:

- `config.env`: the only config file (password, tokens), mode 0600
- `versions/` and the `current` symlink: every installed release and the active one
- `notes/`: your agent's knowledge base
- `cron-jobs/`: scheduled job definitions
- `logs/merlin.log`: the app log (rotating)
- runtime dirs created as needed: `skills/`, `sessions/`, `data/`, `extensions/`

## Mobile notes

- The web terminal attaches to a persistent tmux session: closing the tab or losing signal never kills the shell. Reopen the page and resume exactly where you left off.
- On touch devices a virtual-key toolbar appears (Esc, Tab, Ctrl, Alt, arrows, and more), plus a microphone button for voice input. Details in [terminal.md](terminal.md).
- Copy/paste on mobile needs HTTPS and a tap: use your tunnel URL, not plain http.

## Troubleshooting

- **`merlin: command not found` right after install**: the PATH line went into your shell rc but your current shell predates it. `source` the rc or restart your shell.
- **`Error: fd is not installed`**: fd is a hard requirement; the error message tells you how to install it.
- **tmux missing**: boot prints a warning, the Terminal nav item is grayed out with an install tooltip, everything else still works.
- **cloudflared missing with tunnel enabled**: warning at boot, tunnel silently disabled, dashboard still runs locally.
- **Tunnel enabled but no password set**: Merlin auto-generates one and prints `Auto-generated login: admin / <password>` at startup so the public URL is never unprotected.
- **Wrong password at login**: the form says so and returns 401. The password is whatever you set in `merlin setup`.
- **Quick Tunnel URL keeps changing**: that is how Quick Tunnels work, and `merlin dashboard-url` cannot know the ephemeral URL. Set `MERLIN_DASHBOARD_URL` in `config.env` for a stable address, or move to your own tunnel or Merlin Cloud.
- **Bad update**: revert with `ln -sfn ~/.merlin/versions/<old> ~/.merlin/current`; old versions are never deleted.

## Where to next

- [terminal.md](terminal.md): the web terminal, the front door for working from your phone.
- [bot.md](bot.md): put the same agent in your Discord chat.
- [agents.md](agents.md): how your agent operates Merlin and what it remembers.
