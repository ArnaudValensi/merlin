# Workspace Restore and Recovery: Internals

Implementation reference for the workspace restore. The user guide is the
"Get your workspace back" section of [`docs/terminal.md`](../terminal.md).
The module is `workspace/`, a core module (always active, no flag, no
setting).

```
workspace/
├── snapshot.py   # capture: tmux rows + /proc -> snapshot dict; argv filtering; the server identity
├── store.py      # latest.json, history/, crashes/, the offer, atomic durable writes, retention
├── restore.py    # what is missing, the restore engine, its two guards
├── service.py    # the startup freeze, the sweep, the death detection, the offer, the push
├── routes.py     # /api/workspace/{pending,restore,dismiss}
├── cli.py        # merlin workspace list | show | restore
└── static/
    └── restore.js   # the banner (markup in templates/base.html), its polling
```

The guiding rule is 80/20: cover the common case (power cut, reboot, tmux
dying under a running Merlin) simply, and do not engineer for edge cases.
The non-goals at the end are decisions, not a backlog.

## Capture

`service.run()` sweeps every `SWEEP_INTERVAL` (15 s) in a thread. One sweep is
three tmux calls (`list-sessions`, `list-windows -a`, `list-panes -a`) and one
walk of `/proc`. It writes `latest.json` only when the content changed
(`snapshot.same_content` ignores `saved_at`), and **never** when there is no
tmux server, it holds no session, or any of the three reads failed, so a dead
server or a flaky read never erases the last good snapshot. A power cut loses
at most one interval of changes.

### The server identity

Every list read also asks for `#{pid}` and `#{start_time}`, two server
formats, so the identity of the server costs no extra call. The snapshot
records it as `server: {pid, start_time}`, an optional field (`SNAPSHOT_VERSION`
stays 1, a snapshot written before the field reads fine and simply never
triggers the "server changed" rule). The three reads are separate calls, and
a server can die and be replaced between two of them: a read whose parts name
two servers is a failed read (`read_tmux_status` returns `FAILED`), never a
snapshot.

`read_tmux_status` tells a read that found **no server** from one that
**failed**: tmux's own words for a socket nobody answers on, or a connect
error whose socket is absent or unanswered (the socket itself is probed, not
the localized errno text), mean no server; anything else (a timeout, a
permission error, the test guard refusing a call) is a failed read and says
nothing about the server.

### Agents

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
  "server": {"pid": 12345, "start_time": 1791200000},
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

A crash point and the offer carry one more object, `crash: {reason,
detected_at}` (reasons `startup`, `server-changed`, `server-gone`), and the
offer the crash point's `id`.

`store.read_snapshot` validates the whole shape down to panes and agents (the
fields the engine indexes are required, the ones it reads with a None-check
stay optional and nullable, the two it reads with a typed default, a window's
`layout` and an agent's `options`, must be typed when present), and
`saved_at` must be a finite time. A file that fails any of it reads as absent:
a foreign or torn file never crashes the sweep, the banner or the CLI.

Runs in `merlin-jobs` are not part of the workspace, with one exception: a job window someone stepped into (`@job_stepped_in`), or that its job keeps (`@job_keep_window`), is recorded under a session of its own, `merlin-jobs-kept`. It comes back there like any window, with no `@job_run`, so job retention never touches it and a new run never competes with it. The entry records its `job_run`, and neither the offer nor the restore counts a run whose window is still alive: a mere Merlin restart loses nothing and offers nothing.

## Storage

`paths.workspace_dir()` = `~/.merlin/data/workspace/`, under Merlin's home
rather than `~/.local/state` so that in a Merlin Cloud container it lives on
the persistent volume. Every write goes through `store.write_snapshot`: temp
file, `fsync`, `os.replace`, then `fsync` of the directory (a power cut
mid-write is exactly the case this exists for).

| Path | What | Retention |
|---|---|---|
| `latest.json` | the continuous snapshot | overwritten |
| `history/<id>.json` | a copy of every snapshot the sweep writes | thinned: every entry of the last 2 hours, then the newest of each clock hour up to 7 days, nothing older |
| `crashes/<id>.json` | a crash point: the snapshot a dead server was last seen with, plus its `crash` object | 30 days from the detection, never thinned |
| `pending-restore.json` | the restore offer: a full copy of the crash point plus its `id` | until restored, dismissed, or nothing of it is missing any more |
| `pending.lock` | the advisory lock serializing the offer's publication with its removal | |
| `restore.lock` | the advisory lock of the restore engine | |

**Ids** are the snapshot's `saved_at` in local time, `YYYYMMDD-HHMMSS`,
suffixed `-2`, `-3` on a collision. History and crash ids share one
namespace, so `show` and `restore` take either without a prefix.

**Thinning** (`store.thin`) runs after each history write, from the directory
listing alone for the history (the age is the id's own time); a crash point is
read only once its id is old enough, and expires 30 days after its
`detected_at` (a snapshot can be weeks old when its server dies). A file whose
name is not an id is never touched.

**The offer** is published before its crash point is kept, so the restore
point is safe after the first write and a crash file exists only for a
published offer (the "frozen once" rule below reads the crash points on
disk). `store.archive_pending` completes a crash point the second write
missed, from the offer's own id, reason and detection time: at startup, at
the top of every sweep (before any write), and before the offer is removed or
replaced. An offer from before ids existed is given an id and a `startup`
crash point dated by the file's modification time.

**Removing the offer names the offer** (`store.delete_pending(examined)`):
the sweep can publish a newer crash while the banner's request was reading,
restoring or dismissing the older one, and the newer one must stay. The
publication and the compare-and-delete share `pending.lock` (a blocking
`flock`, held for one file operation, never across a restore, cross-process
because the CLI drops the offer it restored the same way).

## The triggers

### At startup: the freeze

No boot id, no clean-shutdown marker. The rule is "Merlin just started and
sessions are missing". `service.freeze_pending()` runs at the start of the
sweep task, **before its first write**:

1. an existing `pending-restore.json` is kept (Merlin restarted again before
   the user acted), once its archive is complete;
2. else, if `latest.json` holds sessions missing from live tmux
   (`restore.missing_sessions`), it is frozen with reason `startup`.

"Missing" means absent, or present only as the terminal's fresh placeholder (a
fillable `merlin-dev`, see below) while the snapshot's `merlin-dev` held more
than one bare shell. The check is by shape, not name only: a browser tab that
reconnects the instant Merlin starts can create `merlin-dev` before the freeze
runs, and a name check would then see nothing missing and lose the saved
`merlin-dev`. A saved bare `merlin-dev` against a live bare one is not missing.

No sweep writes until the freeze has succeeded: a failed freeze (a full disk)
is retried every interval and `latest.json` is left alone meanwhile.

Outcomes: after a reboot tmux is empty, so an offer is made. A Merlin restart
for an update leaves tmux running, nothing is missing, no offer.

### While Merlin runs: the death detection

This is the case of 2026-11-05: a test killed the user's tmux server while
Merlin kept running, the web terminal recreated an empty server two seconds
later, and the next sweep overwrote `latest.json` with it. The sweep now
detects the death **before it writes**, from the one set of reads it already
makes. Let `disk` be the snapshot `latest.json` holds and `now` the sweep's
read:

- **server changed**: `now` read fine, both snapshots know their server and
  they differ. The server `disk` was taken from is dead.
- **server gone**: `NO_SERVER_SWEEPS` (2) consecutive sweeps found no server
  at all. One miss is never an alert. A read that *failed* counts toward
  nothing, neither way; a server seen resets the count.

On either rule, if `disk` holds sessions missing from live tmux, it is frozen
as a crash point and becomes the offer (replacing a pending one: this is a
newer loss; the older crash point stays in `crashes/`, restorable from the
CLI), and only then does the sweep write the new `latest.json`. A dead server
is frozen once: `detect` keys on the server identity (`store.identity_of`, or
the save time for a snapshot without one) and consults the crash points on
disk the first time it meets an identity, so a Merlin restart, or the startup
freeze followed by the first sweeps after a reboot, never doubles a crash
point. An identity with nothing missing (the user rebuilt everything already)
is marked handled and never looked at again. A deliberate `tmux kill-server`
triggers an offer too, by design: the user dismisses it.

A runtime crash point (`server-changed`, `server-gone`, never `startup`) is
also pushed to every subscribed device (below).

## The offer, the banner, the push

`GET /api/workspace/pending` recomputes on every call the sessions a restore
would bring back now (`restore.missing_sessions` against `restore.live_sessions`)
and, when none are missing any more, drops the offer it examined. It answers
the counts, the session names, `saved_at`, and the offer's `id`, `reason` and
`crashed_at` (null for an offer without a crash object). `POST .../restore`
runs the restore and drops the offer when no session failed (a failed or
partial restore keeps it for what is still missing); it answers 409 when a
restore is already running and 503 when tmux could not be read. `POST
.../dismiss` drops the offer, the one named by the optional body `{"id"}` or
any when a page sends none.

The banner markup sits in `templates/base.html`, in `.main-banners` with the
agent-state consent banner (same `consent-banner` classes, green accent). On
the terminal page `.main-banners` floats over the split on desktop (see
`terminal.html`), so two banners stack instead of overlapping.
`workspace/static/restore.js` fetches the offer on load, then every 15 s while
the banner is hidden and the page visible, and when the page becomes visible
again, so a crash shows up without a reload. A crash offer is titled "tmux
stopped at HH:MM" (the viewer's locale); the startup title stays "Your last
workspace didn't come back". Every field beyond the first release's is read as
optional: the page keeps working against a server that has not restarted yet.
Dismiss sends the offer's id; a failed Restore shows the server's reason.

**The push** (`service.notify_crash`, after the freeze returned, in the
sweep's thread): title "tmux stopped", body "N sessions, M agents lost at
HH:MM. Open Merlin to restore them.", tag `workspace-restore` (a second crash
replaces the first notification), url `/terminal`, no window target (the
worker then navigates to the url), state `crash`, the environment icon, a
**TTL of 12 hours** (a phone off all night still gets it in the morning),
urgency high, no suppression. It goes through `notifications.routes.
get_sender()` and `PushSender.send_all_sync(payload, ttl=...)`, the sender's
seam for a prepared payload with its own TTL (window pushes keep theirs).
Nothing is sent, and no key pair is generated, without a subscription; a
failure is logged and never undoes or delays the freeze. The board's waiting
sync closes only window notifications, so the crash push stays until the user
acts. A power cut or a reboot kills Merlin with tmux: no push is possible then,
the startup banner covers it.

## The restore engine

`restore.restore(snapshot)` is the one engine: the banner and the CLI both
call it. It is the skill's restore, ported. Per snapshot session:

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
PATH apply, and quitting the agent leaves a shell in the right directory.
Nothing is ever typed into a plain pane. One session failing
(`RestoreError`) is logged and reported; the others still restore.

### The guards

- **One restore at a time, across processes.** `restore.lock` under the
  workspace directory, an advisory `flock` taken without waiting for the whole
  restore; a holder in this process or another (the banner, the CLI) makes a
  new restore raise `RestoreBusy` ("a restore is already running") at once.
- **No conversation launched twice, whatever the provider.** A conversation
  is live when a Claude session file names it, when a live window's
  `@agent_provider` / `@agent_conv` names it (the hook stamps both agents, so
  this covers Codex), or when this very restore has just typed it into an
  earlier pane (a snapshot can name one conversation in several panes, since
  Codex is attributed per window). Its pane comes back as a plain shell. A
  session rolled back releases what it typed.
- **A failed tmux read stops the restore before anything.** What is live is
  then unknown, so the engine raises `RestoreUnreadable` before any mutation;
  only a confirmed absence of server means an empty workspace.
- **A session that fails halfway is rolled back**, so it is missing again
  and the offer, which stays on a failure, can restore it on a retry: a
  session this restore created is killed, a placeholder being filled loses
  the windows added and gets its original window back. A tmux id is unique
  within one server only, and a replacement server on the same socket hands
  out the same ids again, so every rollback command runs through `if-shell
  -F` on the pid and start time of the server that created the object, test
  and command evaluated by that server in one round trip (`_owned`): on any
  other server it does nothing.

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

## `merlin workspace`

`workspace/cli.py`, a delegated core command like `merlin review`
(`cli.DELEGATED_COMMANDS`, `ext_commands.CORE_COMMANDS`). It reads the files
under `MERLIN_HOME` and drives tmux through the engine, never the HTTP API, so
it works with Merlin stopped. `list` shows the crash points of the last 30
days (by detection time) and the 20 newest history entries, newest first, the
pending offer marked (`--all`, `--json`). `show <id>` prints one restore point
and which of its sessions are missing from tmux now (unknown when tmux could
not be read). `restore [<id>]` rebuilds the pending offer by default, or a
point by id, limited to `--session` names (an unknown name restores nothing);
`--dry-run` prints what would be created and the exact command typed in each
agent pane, from a read-only tmux read, and touches nothing (an unreadable
tmux is an error, as the engine would refuse that restore). After a successful
restore of the pending point the CLI re-reads what is live and drops the offer
once nothing of it is missing. Exit 1 on a failed session, a held lock or an
unreadable tmux. Syntax: `merlin workspace --help`.

## Tests

- `tests/unit/test_workspace_snapshot.py`: argv filtering, capture from canned
  tmux rows and a fake process tree, the server identity, the read status
  (no server told from a failed read, a mixed read).
- `tests/unit/test_workspace_store.py`: atomic files, the reader's validation,
  ids, thinning, the freeze, the death detection with observations replacing
  tmux, the offer's publication and archive, the crash push.
- `tests/unit/test_workspace_detection.py`: the detection against a real tmux
  isolated by `TMUX_TMPDIR`, the server killed by its socket and replaced, or
  not.
- `tests/unit/test_workspace_restore.py`: restore against a real tmux isolated
  by `TMUX_TMPDIR` (tmux's default socket lives there, so Merlin's bare `tmux`
  calls are isolated with no seam), with stub `claude` / `codex` on PATH that
  record their argv; the guards, the rollbacks, a replacement server; plus a
  capture round trip.
- `tests/unit/test_workspace_cli.py`: the CLI on canned files and an isolated
  tmux, Merlin not running, and from a cold process.
- `tests/unit/test_workspace_routes.py`: the API on the real app.
- `tests/unit/test_board_hooks.py`: the hook stamping and overwriting
  `@agent_conv`.
- `tests/js/notifications-sync.test.js`: the board's waiting sync leaving the
  crash push alone.
- `tests/e2e/test_workspace_restore.py`: a throwaway Merlin seeded with a
  snapshot (`start_merlin(prepare=...)`), banner, Restore, Dismiss, and a crash
  offer appearing without a reload.

Never test a restore, a detection or a kill against the real tmux server:
every real-tmux test runs on the isolated server of `tests/tmux_isolation.py`
and kills it by its socket only (`-S`). See
[`development-setup.md`](development-setup.md).

## Non-goals

Agents cut mid-turn get no special handling. Non-agent commands are never
rerun. No scrollback, no launch-time environment variables. No automatic
restore, ever: detection only makes an offer. No per-window selection in the
banner (the CLI's `--session` is the selection). No telling a deliberate
`kill-server` from a crash. A permission mode or effort changed during a
session is not kept (the snapshot keeps launch options only).
