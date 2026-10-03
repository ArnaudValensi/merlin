#!/usr/bin/env python3
"""Supervise one app: the process that leads its process group.

    supervise.py EXIT_FILE [--audio-sink SINK --audio-status FILE] -- ARGV...

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

With ``--audio-sink``, the app's sound gets a sink first (and its guard, see
sound.py), created through a ``pw-cli`` connection this supervisor holds:
the nodes die with that connection. The holder is in a process group of its
own (Merlin's group signals do not reach it), has a parent-death signal (it
dies with the supervisor), and is signalled only through a pidfd opened
before anything here reaps. While ending, it is spared until no other
process of the app is left, so the app's last sound never falls back to the
speakers; then it is killed. The effective mode, ``stream`` or ``local`` (no
sink: the routing variables are dropped and the app plays on the machine),
goes to the ``--audio-status`` file before the app starts.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time

# Run as a script by sessions.py: the package lives one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import sound  # noqa: E402

PR_SET_CHILD_SUBREAPER = 36
PR_SET_PDEATHSIG = 1
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


def _identity(pid: int) -> tuple[int, int] | None:
    fields = _stat_fields(pid)
    return (pid, int(fields[19])) if fields else None


def _targets(spare: tuple[int, int] | None = None) -> list[int]:
    """Our processes now, without ``spare`` (PID plus start time)."""
    me = os.getpid()
    group = os.getpgid(0)
    return [
        int(entry.name)
        for entry in os.scandir("/proc")
        if entry.name.isdigit()
        and int(entry.name) != me
        and _is_ours(int(entry.name), me, group)
        and (spare is None or _identity(int(entry.name)) != spare)
    ]


def signal_ours(
    sig: int,
    already: set[tuple[int, int]] | None = None,
    spare: tuple[int, int] | None = None,
) -> int:
    """Signal every process that is ours right now (but ``spare``); return
    how many.

    With ``already``, a process (PID plus start time) signalled before is
    skipped, so each gets SIGTERM once.
    """
    me = os.getpid()
    group = os.getpgid(0)
    sent = 0
    for pid in _targets(spare):
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
            if identity == spare:
                continue
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


AUDIO_ENV = ("PULSE_SINK", "PIPEWIRE_NODE", "SDL_AUDIO_DRIVER", "SDL_AUDIODRIVER")
SOUND_TIMEOUT = 4.0  # for both nodes; sessions.py waits 8 s for the verdict


class Holder:
    """The pw-cli connection that owns the app's sound nodes."""

    def __init__(self, proc: subprocess.Popen, fd: int, identity: tuple[int, int]):
        self.proc = proc
        self.fd = fd
        self.identity = identity

    def kill(self) -> None:
        try:
            signal.pidfd_send_signal(self.fd, signal.SIGKILL)
        except OSError:
            pass  # gone already; a pidfd never reaches another process


def _dies_with_us() -> None:
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


def start_sound(sink: str) -> Holder | None:
    """Create the app's sink and its guard; None when they cannot be made
    (the app then plays on the machine).

    Runs before the app and before the reaping loop: nothing else reaps
    here yet, so the pidfd opened right after the fork pins the holder."""
    try:
        proc = subprocess.Popen(
            ["pw-cli"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=sound.ENV,
            process_group=0,
            preexec_fn=_dies_with_us,
        )
    except OSError:
        return None
    fd = os.pidfd_open(proc.pid)
    identity = _identity(proc.pid)
    made = identity is not None and proc.stdin is not None
    deadline = time.monotonic() + SOUND_TIMEOUT
    try:
        for name, spec in sound.specs(sink) if made else []:
            assert proc.stdin is not None
            proc.stdin.write(f"create-node adapter {spec}\n".encode())
            proc.stdin.flush()
            while not sound.ready(sound.graph(deadline - time.monotonic()), name):
                if time.monotonic() > deadline or proc.poll() is not None:
                    made = False
                    break
                time.sleep(0.05)
            if not made:
                break
    except OSError:
        made = False
    if made and identity is not None:
        return Holder(proc, fd, identity)
    if proc.poll() is None:
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    proc.wait()
    os.close(fd)
    return None


def main() -> int:
    args = sys.argv[1:]
    options: dict[str, str] = {}
    exit_file = args.pop(0) if args else ""
    while len(args) >= 2 and args[0] in ("--audio-sink", "--audio-status"):
        options[args[0]] = args[1]
        del args[:2]
    if not exit_file or not args or args[0] != "--" or len(args) < 2:
        print(
            "usage: supervise.py EXIT_FILE [--audio-sink SINK --audio-status FILE]"
            " -- ARGV...",
            file=sys.stderr,
        )
        return 2
    argv = args[1:]
    sink = options.get("--audio-sink", "")
    status_file = options.get("--audio-status", "")

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)

    stopping = False

    def on_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    env = dict(os.environ)
    holder = start_sound(sink) if sink else None
    if sink and holder is None:
        for name in AUDIO_ENV:
            env.pop(name, None)
    if status_file:
        with open(status_file + ".tmp", "w") as handle:
            handle.write("stream\n" if holder else "local\n")
        os.replace(status_file + ".tmp", status_file)

    try:
        main_child = subprocess.Popen(argv, env=env)
    except OSError as exc:
        print(f"supervise: cannot start {argv[0]}: {exc}", file=sys.stderr, flush=True)
        with open(exit_file, "w") as handle:
            handle.write("127\n")
        if holder:
            holder.kill()
        return 127
    spare = holder.identity if holder else None

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
            if spare is not None and not _targets(spare):
                # Nothing of the app is left: now its sound nodes can go.
                assert holder is not None
                holder.kill()
                spare = None
            elif time.monotonic() - ending_since < GRACE:
                signal_ours(signal.SIGTERM, termed, spare)  # newcomers too
            else:
                signal_ours(signal.SIGKILL, spare=spare)  # adopted ones included
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
