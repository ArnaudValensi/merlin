"""``merlin workspace``: the restore points from the shell.

``list`` shows the crash points and the thinned history, ``show`` one of them
with what a restore would bring back now, ``restore`` rebuilds one (the
pending offer by default) through the same engine as the banner, the same
file lock included. It reads the files under ``MERLIN_HOME`` and drives tmux
itself, never the HTTP API, so it works with Merlin stopped: the morning
after a crash at night, or when the dashboard itself is down.

Nothing here restores on its own, and ``--dry-run`` touches nothing: it
prints what would be created and the exact command that would be typed in
each agent pane.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Run through cli.py or as a script: both need the repo root on the path.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import paths  # noqa: E402
from argparse_help import HelpfulParser  # noqa: E402
from terminal.tmux import JOBS_SESSION_NAME  # noqa: E402

from . import restore as rs  # noqa: E402
from . import snapshot as snap  # noqa: E402
from . import store  # noqa: E402

HISTORY_SHOWN = 20
CRASHES_SHOWN_FOR = 30 * 86400
SHORT_ID = 8


class CliError(Exception):
    """A user-facing error: printed on stderr, exit 1."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _when(epoch: float | None) -> str:
    if epoch is None:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _kind(snapshot: dict) -> str:
    crash = store.crash_of(snapshot)
    return f"crash {crash['reason']}" if crash else "history"


def _load(entry_id: str) -> dict:
    """A crash point or history entry by id, with its ``id`` set."""
    path = store.entry_path(entry_id)
    data = store.read_snapshot(path) if path else None
    if data is None:
        raise CliError(f"no restore point {entry_id}: run `{PROG} list` to see them")
    return {**data, "id": entry_id}


def _pending() -> dict | None:
    return store.read_pending()


def _session_names(snapshot: dict) -> list[str]:
    return [s["name"] for s in snapshot["sessions"]]


def _select(snapshot: dict, names: list[str] | None) -> dict:
    """``snapshot`` limited to the sessions named, in the snapshot's order.
    An unknown name is an error: nothing is restored then."""
    if not names:
        return snapshot
    known = set(_session_names(snapshot))
    unknown = [n for n in names if n not in known]
    if unknown:
        raise CliError(
            "unknown session(s): "
            + ", ".join(unknown)
            + " (this restore point holds: "
            + ", ".join(sorted(known))
            + ")"
        )
    wanted = set(names)
    return {
        **snapshot,
        "sessions": [s for s in snapshot["sessions"] if s["name"] in wanted],
    }


class Live:
    """One status-aware read of tmux: the live map, the live job runs and
    the live conversations, or ``unknown`` when tmux answered but could not
    be read whole (the engine would refuse to restore then)."""

    def __init__(self) -> None:
        self.rows, status = snap.read_tmux_status()
        self.unknown = status == snap.FAILED
        self.no_server = status == snap.NO_SERVER
        rows = self.rows
        self.sessions = rs.live_sessions_of(rows) if rows is not None else {}
        self.job_runs = rs.job_runs_of(rows) if rows is not None else set()

    def missing(self, snapshot: dict) -> list[dict]:
        return rs.missing_sessions(snapshot, self.sessions, self.job_runs)


def _plan(snapshot: dict) -> list[str]:
    """What a restore of ``snapshot`` would do right now, one line each, and
    the exact command it would type in each agent pane. Reads tmux, touches
    nothing. A tmux that could not be read is an error, as the engine would
    refuse the restore itself."""
    live_now = Live()
    if live_now.unknown:
        raise CliError("tmux could not be read, no plan: try again")
    rows = live_now.rows
    live = live_now.sessions
    job_runs = live_now.job_runs
    running = rs.live_conversations(rows)
    lines = []
    if rows is None:
        lines.append("no tmux server: it would be started")
    for sess in rs._without_live_kept(snapshot["sessions"], job_runs):
        name = sess["name"]
        if not sess.get("windows") or name == JOBS_SESSION_NAME:
            continue
        if name not in live:
            lines.append(f"create session {name}")
        elif live[name]:
            lines.append(f"fill session {name} (the terminal's empty placeholder)")
        else:
            lines.append(f"skip session {name}: live and in use")
            continue
        for win in sorted(sess["windows"], key=lambda w: w["index"]):
            lines.append(f"  window {win['index']} {win['name']}")
            for pane in sorted(win["panes"], key=lambda p: p["index"]):
                lines.append(f"    pane {pane['index']}  {pane.get('cwd', '')}")
                agent = pane.get("agent")
                if not agent or not agent.get("conv"):
                    continue
                key = (agent["provider"], agent["conv"])
                if key in running:
                    lines.append("      already running elsewhere: plain shell")
                    continue
                running.add(key)
                lines.append("      " + snap.shell_quote(snap.resume_command(agent)))
    return lines


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(args) -> int:
    pending = _pending()
    pending_id = pending.get("id") if pending else None
    now = time.time()
    rows = []
    history_shown = 0
    for entry in store.list_entries():
        data = store.read_snapshot(entry["path"])
        if data is None:
            continue  # torn or foreign: not a restore point
        if entry["kind"] == "crash":
            crash = store.crash_of(data)
            detected = crash["detected_at"] if crash else store.id_time(entry["id"])
            if (
                not args.all
                and detected is not None
                and now - detected > CRASHES_SHOWN_FOR
            ):
                continue
        else:
            history_shown += 1
            if not args.all and history_shown > HISTORY_SHOWN:
                continue
        windows, agents = snap.counts(data["sessions"])
        rows.append(
            {
                "id": entry["id"],
                "saved_at": data["saved_at"],
                "kind": entry["kind"],
                "reason": (store.crash_of(data) or {}).get("reason"),
                "sessions": len(data["sessions"]),
                "windows": windows,
                "agents": agents,
                "pending": entry["id"] == pending_id,
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no restore points yet")
        return 0
    for r in rows:
        kind = f"crash {r['reason']}" if r["kind"] == "crash" else "history"
        print(
            f"{r['id']:<18} {_when(r['saved_at'])}  {kind:<22} "
            f"{r['sessions']:>2} sessions {r['windows']:>3} windows {r['agents']:>3} agents"
            + ("  * pending offer" if r["pending"] else "")
        )
    return 0


def cmd_show(args) -> int:
    snapshot = _load(args.id)
    live_now = Live()
    missing = (
        None if live_now.unknown else {s["name"] for s in live_now.missing(snapshot)}
    )
    if args.json:
        record = {**snapshot, "missing": None if missing is None else sorted(missing)}
        print(json.dumps(record, indent=2))
        return 0
    crash = store.crash_of(snapshot)
    print(f"{snapshot['id']}  saved {_when(snapshot['saved_at'])}  {_kind(snapshot)}")
    if crash:
        print(f"detected {_when(crash['detected_at'])}")
    pending = _pending()
    if pending and pending.get("id") == snapshot["id"]:
        print("this is the pending restore offer")
    if missing is None:
        print("live state unknown: tmux could not be read, try again")
    for sess in snapshot["sessions"]:
        if missing is None:
            state = "unknown"
        else:
            state = (
                "missing from tmux now" if sess["name"] in missing else "live in tmux"
            )
        print(f"\nsession {sess['name']}  ({state})")
        for win in sorted(sess["windows"], key=lambda w: w["index"]):
            print(f"  window {win['index']} {win['name']}")
            for pane in sorted(win["panes"], key=lambda p: p["index"]):
                line = f"    pane {pane['index']}  {pane.get('cwd', '')}"
                agent = pane.get("agent")
                if agent:
                    options = " ".join(agent.get("options") or [])
                    line += (
                        f"  [{agent['provider']} {agent['conv'][:SHORT_ID]}"
                        + (f" {options}" if options else "")
                        + "]"
                    )
                print(line)
    return 0


def cmd_restore(args) -> int:
    pending = _pending()
    if args.id:
        snapshot = _load(args.id)
        is_offer = bool(pending) and pending.get("id") == args.id
    elif pending is None:
        raise CliError(
            f"no pending restore offer: run `{PROG} list` and give an id to restore"
        )
    else:
        snapshot = pending
        is_offer = True
    selected = _select(snapshot, args.session)
    if args.dry_run:
        print(
            f"dry run of {snapshot.get('id', 'the pending offer')}: nothing is touched"
        )
        for line in _plan(selected):
            print(line)
        return 0
    try:
        result = rs.restore(selected)
    except rs.RestoreError as exc:  # busy, unreadable
        raise CliError(str(exc)) from exc
    if is_offer and not result["failed"]:
        # The offer is done once nothing of it is missing any more, whether
        # this command restored all of it, the last part of it, or the rest
        # was live already. Unknown live state keeps it.
        live_now = Live()
        if not live_now.unknown and not live_now.missing(snapshot):
            store.delete_pending(snapshot.get("id"))
    print(
        f"restored {len(result['restored'])} session(s), "
        f"{result['agents']} agent(s) resumed"
        + (": " + ", ".join(result["restored"]) if result["restored"] else "")
    )
    if result["skipped"]:
        print("skipped (name in use): " + ", ".join(result["skipped"]))
    if result["failed"]:
        print("failed: " + ", ".join(result["failed"]), file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

PROG = "merlin workspace"


def build_parser(prog: str = PROG) -> argparse.ArgumentParser:
    parser = HelpfulParser(
        prog=prog,
        description=(
            "The restore points of your tmux workspace: the crash points Merlin "
            "froze when tmux died, and the thinned history of its snapshots. "
            "Works with Merlin stopped."
        ),
        epilog="""
Examples:
  merlin workspace list                          crash points and recent history
  merlin workspace show 20261105-185945          what that point holds, what is missing
  merlin workspace restore                       the pending offer, as the banner's Restore
  merlin workspace restore 20261105-185945 --dry-run
  merlin workspace restore 20261105-185945 --session dev --session oob

Nothing is restored on its own. A restore never launches a conversation that
is already running, and one restore runs at a time.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    p = sub.add_parser("list", help="The restore points, newest first")
    p.add_argument("--all", action="store_true", help="every history entry")
    p.add_argument("--json", action="store_true", help="raw records")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="One restore point and what is missing now")
    p.add_argument("id", help="a crash point or history id (see list)")
    p.add_argument("--json", action="store_true", help="the raw snapshot")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser(
        "restore", help="Rebuild a restore point (default: the pending offer)"
    )
    p.add_argument("id", nargs="?", help="a crash point or history id (see list)")
    p.add_argument(
        "--session",
        action="append",
        metavar="NAME",
        help="only this session (repeatable)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be created and typed, touch nothing",
    )
    p.set_defaults(func=cmd_restore)
    return parser


def main(argv: list[str] | None = None, prog: str = PROG) -> int:
    global PROG
    PROG = prog
    paths.load_config_env()
    parser = build_parser(prog)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], prog="workspace/cli.py"))
