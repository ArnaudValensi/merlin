# Getting Started

Merlin runs as one process on your machine: a web dashboard on port 3123 with a web terminal, files, commits, notes, and jobs, behind a password login. This page takes you from a fresh machine to that dashboard in your pocket. Everything else builds on what you set up here: the one process you start hosts all three pillars, and the `~/.merlin` home the installer creates is where your notes, jobs, and config live.

![Merlin running on a phone](terminal/phone-merlin.jpg)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/ArnaudValensi/merlin/master/install.sh | bash
```

The installer needs `uv` (it offers to install it if missing) and checks for two more tools, offering to install what is missing: fd (required, Merlin will not start without it) and tmux (optional, needed for the web terminal). It downloads the latest release into `~/.merlin/versions/<tag>/`, points the `~/.merlin/current` symlink at it, and offers to add `~/.merlin/current/bin` to your PATH in your shell rc.

When it finishes: run `merlin` to start (you may need to restart your shell first).

## Run the setup wizard

Bare `merlin` runs the wizard automatically on first start when no config exists yet. You can also run it any time:

```bash
merlin setup
```

It asks, in order:

- **Dashboard password**: empty means no auth, fine for local-only use.
- **Discord bot token**: Enter to skip; you can add it later (see [bot.md](bot.md)).
- **OpenAI API key for voice transcription**: Enter to skip. Without it, transcription falls back to a local model (~1.5GB download, works offline); with it, the Whisper API costs about $0.006/min.

Answers land in `~/.merlin/config.env` (mode 0600, the only config file). Re-running setup shows your current values as defaults and asks before overwriting. The wizard also aggregates your agent skills so they are ready before the first run.

## Start and log in

```bash
merlin
```

Bare `merlin` is `merlin start`. It prints `Merlin starting on http://0.0.0.0:3123` plus the working directory it serves. Open **http://localhost:3123**: you get a single password field, and a successful login sets a signed cookie that lasts 30 days and survives restarts. Log out at `/logout`. Port, host, and the rest are in `merlin start --help`.

That one process is the whole system: the job scheduler starts with it, extensions load, and the skill registry your agent reads is rebuilt. The terminal, the bot, and jobs all share the notes under `~/.merlin`, which is why the longer it runs, the more your agent knows.

## Run Merlin as a service (systemd / launchd)

For a machine that should keep Merlin up 24/7 — restarting it on crash and on
boot — run it under your OS service manager instead of a terminal. This is the
recommended way to run a permanent server.

**Who restarts Merlin.** By default, Merlin owns its own lifecycle: when you
restart or update, it stops the old process and launches the new one itself.
Under a service manager that is wrong — the manager *also* relaunches Merlin, and
two instances fight over the port. So you tell Merlin a supervisor is in charge
with one environment variable:

```
MERLIN_SUPERVISED=1
```

With it set, restart and update **stop and exit**, and let the service manager
start the replacement. Two rules make this safe:

- **Set it in the service unit, never in `config.env`.** `config.env` is read by
  every launch, so a stray `MERLIN_SUPERVISED=1` there would make a hand-run
  `merlin start` think it is supervised and never come back after an update.
- **Your unit must restart Merlin on exit** (`Restart=always` on systemd,
  `KeepAlive=true` on launchd). An in-dashboard update stops Merlin and trusts
  the manager to bring the new version up; a unit that only restarts on failure
  would leave Merlin down — and Merlin is often your only way back into the box.

Ready-to-use templates live in `~/.merlin/current/deploy/`.

**Linux (systemd user service):**

```bash
mkdir -p ~/.config/systemd/user
cp ~/.merlin/current/deploy/merlin.service ~/.config/systemd/user/merlin.service
systemctl --user daemon-reload
systemctl --user enable --now merlin
loginctl enable-linger "$USER"        # keep it running with no active login
# manage it:
systemctl --user restart merlin
journalctl --user -u merlin -f
```

**macOS (launchd user agent):**

```bash
cp ~/.merlin/current/deploy/com.merlin.plist ~/Library/LaunchAgents/com.merlin.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.merlin.plist
launchctl kickstart -k gui/$(id -u)/com.merlin   # restart
```

Both templates already set `MERLIN_SUPERVISED=1` and an always-restart policy.
Settings → Version shows the current runtime mode (supervised or self-managed)
read-only, so you can confirm which regime is live.

Not running under a supervisor? You do not need any of this — `merlin` in a
terminal (or under tmux) restarts and updates itself, exactly as before.

## Reach it from your phone

Expose the dashboard with your own tunnel or reverse proxy (Tailscale, Cloudflare, nginx, whatever you trust), or use Merlin Cloud (next section), which handles remote access for you. Merlin serves plain HTTP on port 3123 and does not ship a tunnel of its own: whatever fronts it terminates HTTPS. If you set a dashboard password, exposure is safe; without one, keep it local.

Earlier versions bundled a cloudflared Quick Tunnel; it has been removed. If your `config.env` still has `TUNNEL_ENABLED`, `TUNNEL_TOKEN`, or `TUNNEL_HOSTNAME`, they are ignored, and re-running `merlin setup` cleans them out. To keep using Cloudflare, run cloudflared yourself pointing at `http://localhost:3123`.

`merlin dashboard-url` prints your dashboard address with credentials embedded when a password is set. It resolves `MERLIN_DASHBOARD_URL` first, then `http://localhost:3123`. If you front Merlin with your own tunnel or proxy, set `MERLIN_DASHBOARD_URL` in `config.env` so the command (and your agent) know the stable address.

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

`merlin version` prints the active version. `merlin config` lists every resolved path (notes dir, logs, jobs, sessions, ...); `merlin config <key>` prints one value for use in shell commands.

Inside `~/.merlin`:

- `config.env`: the only config file (password, tokens), mode 0600
- `versions/` and the `current` symlink: every installed release and the active one
- `notes/`: your agent's knowledge base
- `jobs/`: scheduled job definitions
- `logs/merlin.log`: the app log (rotating)
- runtime dirs created as needed: `skills/`, `sessions/`, `data/`, `extensions/`

## Mobile notes

- The web terminal attaches to a persistent tmux session: closing the tab or losing signal never kills the shell. Reopen the page and resume exactly where you left off.
- On touch devices a virtual-key toolbar appears (Esc, Tab, Ctrl, Alt, arrows, and more), plus a microphone button for voice input. Details in [terminal.md](terminal.md).
- Copy/paste on mobile needs HTTPS and a tap: use your tunnel URL, not plain http.

## Troubleshooting

- **`merlin: command not found` right after install**: the PATH line went into your shell rc but your current shell predates it. `source` the rc or restart your shell.
- **`Error: fd is not installed`**: fd is a hard requirement (the installer offers it); the error message tells you how to install it.
- **tmux missing**: boot prints a warning, the Terminal nav item is grayed out with an install tooltip, everything else still works.
- **Wrong password at login**: the form says so and returns 401. The password is whatever you set in `merlin setup`.
- **`merlin dashboard-url` prints localhost but you reach Merlin through a tunnel**: the command cannot know your tunnel's address. Set `MERLIN_DASHBOARD_URL` in `config.env` to the public URL.
- **Bad update**: revert with `ln -sfn ~/.merlin/versions/<old> ~/.merlin/current`; old versions are never deleted.

## Where to next

- [terminal.md](terminal.md): the web terminal, the front door for working from your phone.
- [bot.md](bot.md): put the same agent in your Discord chat.
- [agents.md](agents.md): how your agent operates Merlin and what it remembers.
