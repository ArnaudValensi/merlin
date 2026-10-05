# Workspace Restore: Internals

Implementation reference for the workspace restore. The user guide is the
"Get your workspace back after a restart" section of
[`docs/terminal.md`](../terminal.md). The module is `workspace/`, a core module
(always active, no flag, no setting).

```
workspace/
├── snapshot.py   # capture: tmux rows + /proc -> snapshot dict; argv filtering
├── store.py      # latest.json / pending-restore.json, atomic durable writes
├── restore.py    # what is missing, the restore engine
├── service.py    # startup freeze, 15 s sweep loop, the offer the banner reads
├── routes.py     # /api/workspace/{pending,restore,dismiss}
└── static/
    └── restore.js   # the banner (markup in templates/base.html)
```

The guiding rule is 80/20: cover the common case (power cut, reboot) simply,
and do not engineer for edge cases. The non-goals at the end are decisions,
not a backlog.

## Capture

`service.run()` sweeps every `SWEEP_INTERVAL` (15 s) in a thread. One sweep is
three tmux calls (`list-sessions`, `list-windows -a`, `list-panes -a`) and one
walk of `/proc`. It writes `latest.json` only when the content changed
(`snapshot.same_content` ignores `saved_at`), and **never** when there is no
tmux server, it holds no session, or any of the three reads failed
(`read_tmux` returns `None`, never empty rows), so a dead server or a flaky read
never erases the last good snapshot. A power cut loses at most one interval of changes.

For each pane, `_find_agent` walks the pane's process subtree (breadth-first,
from `pane_pid`) for the first resumable agent:

- **Claude**: an argv whose basename is `claude`, or a pid with a live
  `~/.claude/sessions/<pid>.json`. `-p/--print` runs are skipped. The
  conversation id comes from that session file (exact, per process), else
  from the window's `@agent_conv` when `@agent_provider` is `claude`. The
  relaunch directory is the session file's `cwd` (`claude --resume` only finds
  a conversation from its project directory).
- **Codex**: an argv `codex ...` or `node|bun .../codex ...`, TUI only
  (`codex exec` & co. are skipped). The thread id comes from the window's
  `@agent_conv` when `@agent_provider` is `codex`, else from a launch as
  `codex resume <uuid>`. Otherwise the pane is a plain shell. The relaunch
  directory is the process's cwd.

Launch options are kept from the agent's argv (`--model`, `--effort`,
`--dangerously-skip-permissions`, `--yolo`, `-c key=value`...). The initial
prompt and the options that pick or create a conversation (`--resume`,
`--continue`, `--session-id`, `--worktree`, `--name`...) are dropped. The
tables and `split_cli` come from the `reboot-restore` skill, where they were
proven on real launch lines.

### `@agent_provider` / `@agent_conv`

Stamped on the agent's window by the SessionStart companion hook,
`terminal/hooks/agent-session-init.sh <provider>`, installed for both agents by
`lib/skills.py` (the provider is the hook command's argument, `_provider_of`
picks it from the event table). The hook reads `session_id` from the hook JSON
on stdin with a bash regex (no `jq`). Unlike `@agent_sid`, `@agent_conv` is
**overwritten on every SessionStart**: `/clear` and resume change the
conversation. This replaces the skill's Codex heuristics (thread lock files,
pane title matched against `~/.codex/state_*.sqlite`), which are not ported.

### Snapshot format

```json
{
  "version": 1,
  "saved_at": 1791200000.0,
  "sessions": [
    {
      "name": "merlin-dev",
      "active_window": 3,
      "windows": [
        {
          "index": 3, "name": "portal", "automatic_rename": false,
          "layout": "<tmux window_layout>",
          "options": {"@agent_sid": "...", "@agent_cwd": "..."},
          "panes": [
            {"index": 0, "cwd": "/home/u/proj", "active": true,
             "agent": {"provider": "claude", "conv": "<id>", "cwd": "/home/u/proj", "options": ["--model", "opus"]}}
          ]
        }
      ]
    }
  ]
}
```

`agent` is absent on a plain pane. `options` carries only `@agent_sid`,
`@agent_cwd`, `@agent_parent` and `@agent_relation` (non-empty), so the
Sessions board keeps identities and family links. `@agent_state` is
transient, and provider/conv are restamped by the resumed agent's hook.

## Storage

`paths.workspace_dir()` = `~/.merlin/data/workspace/`, under Merlin's home
rather than `~/.local/state` so that in a Merlin Cloud container it lives on
the persistent volume. Two files, both written temp file, `fsync`,
`os.replace`, then `fsync` of the directory (a power cut mid-write is exactly
the case this exists for). A file that fails to parse, or has another
`version`, reads as absent.

- `latest.json`: the continuous snapshot.
- `pending-restore.json`: the restore offer.

## The trigger: freeze at startup

No boot id, no clean-shutdown marker. The rule is "Merlin just started and
sessions are missing". `service.freeze_pending()` runs at the start of the
sweep task, **before its first write**:

1. an existing `pending-restore.json` is kept (Merlin restarted again before
   the user acted);
2. else, if `latest.json` holds sessions missing from live tmux
   (`restore.missing_sessions`), it is copied to `pending-restore.json`.

"Missing" means absent, or present only as the terminal's fresh placeholder (a
fillable `merlin-dev`, see below) while the snapshot's `merlin-dev` held more
than one bare shell. The check is by shape, not name only: a browser tab that
reconnects the instant Merlin starts can create `merlin-dev` before the freeze
runs, and a name check would then see nothing missing and lose the saved
`merlin-dev`. A saved bare `merlin-dev` against a live bare one is not missing.

No sweep writes until the freeze has succeeded: a failed freeze (a full disk)
is retried every interval and `latest.json` is left alone meanwhile. The
freeze is what keeps the restore point alive. Right after a reboot the
first sweep would otherwise overwrite `latest.json` with the empty workspace,
or with the lone `merlin-dev` the web terminal creates when opened. After the
freeze the sweep overwrites `latest.json` freely.

Outcomes: after a reboot tmux is empty, so an offer is made. A Merlin restart
for an update leaves tmux running, nothing is missing, no offer. A deliberate
`tmux kill-server` makes an offer too; the user dismisses it.

## The offer and the banner

`GET /api/workspace/pending` recomputes on every call the sessions a restore
would bring back now (`restore.missing_sessions` against `restore.live_sessions`)
and, when none are missing any more, deletes the offer. `POST .../restore` runs
the restore and deletes the offer, `POST .../dismiss` deletes it. A lock makes a
double click restore once.

The banner markup sits in `templates/base.html`, in `.main-banners` with the
agent-state consent banner (same `consent-banner` classes, green accent). On
the terminal page `.main-banners` floats over the split on desktop (see
`terminal.html`), so two banners stack instead of overlapping.
`workspace/static/restore.js` fetches the offer on every page load, fills the
counts, session names and save time, and after Restore shows the result for
a few seconds. The Sessions panel picks up the new sessions on its next poll.

## The restore engine

`restore.restore(snapshot)` is the skill's restore, ported. Per snapshot
session:

- **absent**: `new-session -d` sized to the first window's layout
  (`layout_size`) so `select-layout` applies, then each window at its original
  index (`move-window` when the base index differs), panes split in their
  directories with a `tiled` rebalance after each split (splitting the newest
  pane again and again halves it until tmux has no room left), the saved layout
  applied (a failure is logged, the panes stay tiled), `automatic-rename` and the `@agent_*` options
  restamped, the active pane and window selected;
- **fillable** (named `DEFAULT_SESSION_NAME` from `terminal/tmux.py`, the
  session the web terminal auto-creates, and still one window with one pane
  running a bare shell; no other name is ever fillable): the
  original window is parked above every snapshot index, the snapshot's windows
  are created inside the session, the active window selected, then the original
  window is killed if it is still one bare shell (the user may have started
  something in it while the restore ran; then it is kept). The attached client
  moves to a restored window;
- **live with work in it**: skipped, reported by name.

Each agent is typed into its pane's fresh interactive shell (`send-keys -l`,
then Enter, 0.3 s apart), never exec'd, so the user's shell config, aliases and
PATH apply, and quitting the agent leaves a shell in the right directory. A
Claude conversation already running (its id in a live session file) is not
launched again. Nothing is ever typed into a plain pane. One session failing
(`RestoreError`) is logged and reported; the others still restore.

Every tmux call carries `terminal.tmux.tmux_conf_args()` and runs with
`terminal_process_env()`, because when no server exists the restore creates it,
and tmux reads its config (and `MERLIN_TERMINAL_HOOKS`) only at server
creation. Without them the restored server would run with no agent-state pills.

**Supervised installs.** Under systemd (`MERLIN_SUPERVISED=1`, the unit sets no
`KillMode`) a process Merlin spawns lives in the service's cgroup and dies when
the service restarts, taking the whole tmux workspace with it. Every entry
point that can create the tmux server (the web terminal's client, the Sessions
panel's create-session, the restore) runs that call under
`systemd-run --user --scope` via `terminal.tmux.server_spawn_prefix`, so the
server lives in its own transient scope and survives a Merlin restart or
update. Verified with a transient user service: a tmux started plainly from it
died on `systemctl --user stop`, the scoped one survived. Attaching to a
running server needs no prefix.

## Tests

- `tests/unit/test_workspace_snapshot.py`: argv filtering, capture from canned
  tmux rows and a fake process tree.
- `tests/unit/test_workspace_store.py`: atomic files, the freeze, change
  detection, never writing on an empty server, the offer.
- `tests/unit/test_workspace_restore.py`: restore against a real tmux isolated
  by `TMUX_TMPDIR` (tmux's default socket lives there, so Merlin's bare `tmux`
  calls are isolated with no seam), with stub `claude` / `codex` on PATH that
  record their argv; plus a capture round trip.
- `tests/unit/test_board_hooks.py`: the hook stamping and overwriting
  `@agent_conv`.
- `tests/e2e/test_workspace_restore.py`: a throwaway Merlin seeded with a
  snapshot (`start_merlin(prepare=...)`), banner, Restore, Dismiss.

Never test a restore against the real tmux server.

## Non-goals

Agents cut mid-turn get no special handling. Non-agent commands are never
rerun. No scrollback, no launch-time environment variables, no snapshot
history, no CLI command, no per-session selection, no push notification, no
detection of a deliberate `kill-server`.
