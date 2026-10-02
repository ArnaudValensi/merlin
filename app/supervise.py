#!/usr/bin/env python3
"""Supervise one app: the process that leads its process group.

    supervise.py EXIT_FILE [--audio-sink SINK] -- ARGV...

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

With ``--audio-sink``, the app's sound streams also go to that sink while it
runs: the environment (PULSE_SINK...) routes most apps there, but some open
the machine's default output by name (SDL3, and SDL2 through sdl2-compat),
so every playback stream a process of the app opens is moved to the sink.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
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


def _belongs(pid: int, me: int, group: int) -> bool:
    """``pid`` is the app's: in our group, or descended from us (a process
    that left the group with setsid is still our descendant, or adopted)."""
    for _ in range(64):
        if pid == me:
            return True
        fields = _stat_fields(pid)
        if not fields:
            return False
        if int(fields[2]) == group:
            return True
        pid = int(fields[1])
        if pid <= 1:
            return False
    return False


class SoundMover:
    """Keep the app's playback streams on its sink (``pactl subscribe``)."""

    SAFETY_SCAN = 5.0  # a rescan even without events (a missed one)

    def __init__(self, sink: str) -> None:
        self.sink = sink
        self.stopped = threading.Event()
        self.dirty = threading.Event()
        self.env = {**os.environ, "LC_ALL": "C"}
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._mover, daemon=True).start()

    def stop(self) -> None:
        with self.lock:
            self.stopped.set()
            self.dirty.set()
            if self.proc is not None:
                try:
                    self.proc.kill()
                except OSError:
                    pass

    def _pactl(self, *args: str) -> str:
        # Our own waitpid(-1) may reap this child first: subprocess then
        # reports status 0, and the output, read to EOF, is complete anyway.
        try:
            return subprocess.run(
                ["pactl", *args],
                capture_output=True,
                text=True,
                timeout=5,
                env=self.env,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def _listen(self) -> None:
        while True:
            with self.lock:  # never a new subscriber after stop()
                if self.stopped.is_set():
                    return
                try:
                    proc = self.proc = subprocess.Popen(
                        ["pactl", "subscribe"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        env=self.env,
                    )
                except OSError:
                    return
            self.dirty.set()  # a scan once connected: streams opened before
            assert proc.stdout is not None
            for line in proc.stdout:
                if "sink-input" in line:
                    self.dirty.set()
                if self.stopped.is_set():
                    break
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
            self.stopped.wait(1.0)  # the sound server went away: retry

    def _mover(self) -> None:
        me, group = os.getpid(), os.getpgid(0)
        while not self.stopped.is_set():
            self.dirty.wait(self.SAFETY_SCAN)
            if self.stopped.is_set():
                return
            self.dirty.clear()
            time.sleep(0.05)  # coalesce a burst of events
            self.scan(me, group)

    def scan(self, me: int, group: int) -> None:
        try:
            sinks = json.loads(self._pactl("-f", "json", "list", "sinks") or "[]")
            inputs = json.loads(
                self._pactl("-f", "json", "list", "sink-inputs") or "[]"
            )
        except ValueError:
            return
        target = next(
            (s.get("index") for s in sinks if s.get("name") == self.sink), None
        )
        if target is None:
            return  # gone (the app is ending): never move anything elsewhere
        for stream in inputs:
            if self.stopped.is_set() or stream.get("sink") == target:
                continue
            pid = str(stream.get("properties", {}).get("application.process.id", ""))
            if pid.isdigit() and _belongs(int(pid), me, group):
                self._pactl("move-sink-input", str(stream.get("index")), self.sink)


def main() -> int:
    args = sys.argv[1:]
    sink = ""
    if len(args) >= 3 and args[1] == "--audio-sink":
        sink = args[2]
        args = [args[0], *args[3:]]
    if len(args) < 3 or args[1] != "--":
        print(
            "usage: supervise.py EXIT_FILE [--audio-sink SINK] -- ARGV...",
            file=sys.stderr,
        )
        return 2
    exit_file, argv = args[0], args[2:]

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

    # Its first scan, once subscribed, catches a stream opened before it.
    mover = SoundMover(sink) if sink else None
    if mover:
        mover.start()

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
            if mover:
                mover.stop()  # its pactl children end with the passes below
        if ending_since is not None:
            if time.monotonic() - ending_since < GRACE:
                signal_ours(signal.SIGTERM, termed)  # newcomers get theirs too
            else:
                signal_ours(signal.SIGKILL)  # every pass: adopted ones included
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
