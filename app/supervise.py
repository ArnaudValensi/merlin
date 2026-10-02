#!/usr/bin/env python3
"""Supervise one app: the process that leads its process group.

    supervise.py EXIT_FILE -- ARGV...

Standard library only. It becomes a child subreaper, starts ARGV, and stays
alive until every process the app started is gone: orphans (a parent that
exited first, a daemon that double-forked, even one that left the group with
setsid) are re-parented to it and reaped by it. So while anything of the app
lives, its leader lives too, and the group's number cannot be reused: Merlin
proves ownership by the leader's PID *and* start time, and never signals a
group whose leader is gone.

When the app's main process exits, its exit code (shell style: 128+N for a
signal) goes to EXIT_FILE and everything left is ended. A SIGTERM to the
supervisor (Merlin's stop) does the same. Ending is a loop, not one pass: a
process killed in a pass may leave children of its own (in another group,
say) that the subreaper adopts next; each pass finds the current ones.
Processes that appear get SIGTERM once; after GRACE every pass sends SIGKILL.

Individual processes are signalled through pidfds, never bare PIDs: a PID
read from /proc may be reused before the signal is sent, a pidfd may not. Each
pidfd is checked against /proc after opening (still in our group, or still
our child) before it is used.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time

PR_SET_CHILD_SUBREAPER = 36
GRACE = 3.0
POLL = 0.05


def _stat_fields(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/stat") as handle:
            stat = handle.read()
    except OSError:
        return None
    return stat[stat.rfind(")") + 2 :].split() or None


def _is_ours(pid: int, me: int, group: int) -> bool:
    """Live, and in our group or our direct child (an adopted orphan)."""
    fields = _stat_fields(pid)
    if not fields or fields[0] == "Z":
        return False
    return int(fields[2]) == group or int(fields[1]) == me


def _targets() -> list[int]:
    me = os.getpid()
    group = os.getpgid(0)
    return [
        int(entry.name)
        for entry in os.scandir("/proc")
        if entry.name.isdigit()
        and int(entry.name) != me
        and _is_ours(int(entry.name), me, group)
    ]


def signal_ours(sig: int, already: set[tuple[int, int]] | None = None) -> int:
    """Signal every process that is ours right now; return how many.

    With ``already``, a process (PID plus start time) signalled before is
    skipped, so each gets SIGTERM once.
    """
    me = os.getpid()
    group = os.getpgid(0)
    sent = 0
    for pid in _targets():
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            continue  # gone already
        try:
            # The pidfd pins one process: check *that* one is still ours.
            fields = _stat_fields(pid)
            if not fields or not _is_ours(pid, me, group):
                continue
            identity = (pid, int(fields[19]))
            if already is not None:
                if identity in already:
                    continue
                already.add(identity)
            signal.pidfd_send_signal(fd, sig)
            sent += 1
        except OSError:
            pass
        finally:
            os.close(fd)
    return sent


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        print("usage: supervise.py EXIT_FILE -- ARGV...", file=sys.stderr)
        return 2
    exit_file, argv = sys.argv[1], sys.argv[3:]

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)

    stopping = False

    def on_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    try:
        main_child = subprocess.Popen(argv)
    except OSError as exc:
        print(f"supervise: cannot start {argv[0]}: {exc}", file=sys.stderr, flush=True)
        with open(exit_file, "w") as handle:
            handle.write("127\n")
        return 127

    code: int | None = None
    ending_since: float | None = None
    termed: set[tuple[int, int]] = set()
    while True:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return code if code is not None else 0  # nothing of the app left
            if pid == 0:
                break
            if pid == main_child.pid:
                main_child.returncode = os.waitstatus_to_exitcode(status)
                code = main_child.returncode
                if code < 0:
                    code = 128 - code
                with open(exit_file, "w") as handle:
                    handle.write(f"{code}\n")
        if (stopping or code is not None) and ending_since is None:
            ending_since = time.monotonic()
        if ending_since is not None:
            if time.monotonic() - ending_since < GRACE:
                signal_ours(signal.SIGTERM, termed)  # newcomers get theirs too
            else:
                signal_ours(signal.SIGKILL)  # every pass: adopted ones included
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
