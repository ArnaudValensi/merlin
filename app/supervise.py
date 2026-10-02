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
signal) goes to EXIT_FILE and everything left is ended: SIGTERM, then SIGKILL
after a grace. A SIGTERM to the supervisor (Merlin's stop) does the same.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time

PR_SET_CHILD_SUBREAPER = 36
GRACE = 3.0  # below Merlin's STOP_GRACE, so the supervisor cleans up first
POLL = 0.05


def _stat_fields(pid: int) -> list[str] | None:
    try:
        stat = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    return stat[stat.rfind(")") + 2 :].split() or None


def _ours() -> list[int]:
    """Live processes to end: our group's members and our own children."""
    me = os.getpid()
    group = os.getpgid(0)
    out = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        fields = _stat_fields(int(entry.name))
        if not fields or fields[0] == "Z":
            continue
        if int(fields[2]) == group or int(fields[1]) == me:
            out.append(int(entry.name))
    return out


def _signal_all(sig: int) -> None:
    for pid in _ours():
        try:
            os.kill(pid, sig)
        except OSError:
            pass


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
    killed = False
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
            _signal_all(signal.SIGTERM)
        elif ending_since is not None and not killed:
            if time.monotonic() - ending_since > GRACE:
                _signal_all(signal.SIGKILL)
                killed = True
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
