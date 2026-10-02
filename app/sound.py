#!/usr/bin/env python3
"""The sound of one app: its private sink, and every stream it plays kept there.

    sound.py SINK DESCRIPTION

Started by the app's supervisor (supervise.py), in the app's process group,
before the app; it prints ``ok`` once the sink exists (``failed`` otherwise,
and exits). Standard library only; needs PipeWire's tools (pw-cli, pw-dump,
pw-metadata) and pactl.

- **The sink is owned, not loaded.** It is a PipeWire null node created
  through a ``pw-cli`` connection this process keeps open, with
  ``object.linger=false``: PipeWire destroys it when that connection closes.
  pw-cli dies with this process (a parent-death signal), and this one with
  the supervisor, so the sink lives exactly as long as the app. Nothing ever
  unloads a module by number.
- **It is never a default.** Its class is ``Audio/Sink/Virtual``; WirePlumber
  picks the default output among ``Audio/Sink`` and ``Audio/Duplex`` nodes
  (and the default input among sources and ``Audio/Sink`` monitors), so when
  the machine's real outputs go away no other program's sound lands here.
  ``state.restore-*`` off: WirePlumber keeps no state for it. The streamer
  captures this node, and only this one.
- **Apps land next door.** PipeWire's pulse server only starts a stream on a
  sink pulse can list, and it lists only ``Audio/Sink`` nodes: a stream
  pointed at the private sink by name never starts. So the same connection
  holds a landing sink, ``<SINK>_in`` (``Audio/Sink``), where the
  environment (PULSE_SINK...) points the app. It is never captured: if it
  ever became the default output (no other one left), what lands there is
  silenced, never streamed.
- **The app's streams follow the private sink.** On every stream event
  (``pactl subscribe``, plus a rescan every few seconds) each playback stream
  of a process of the app (in its process group, or descended from the
  supervisor) that is not on the private sink (on the landing sink, or on an
  output it named, as SDL3 does with the default one) is moved there with
  ``pw-metadata``, naming the sink: given a name rather than a serial,
  WirePlumber stores no target it could later restore onto another program's
  streams. A stream's first moments, before the move, are not streamed.
- SIGTERM (the supervisor ending the app) waits briefly for the app's streams
  to close, so their last sound is not sent to the speakers, then ends.
"""

from __future__ import annotations

import ctypes
import json
import os
import select
import signal
import subprocess
import sys
import time

PR_SET_PDEATHSIG = 1
SINK_CLASS = "Audio/Sink/Virtual"
LANDING_CLASS = "Audio/Sink"
NODE_TIMEOUT = 3.0  # for the sinks to appear
SAFETY_SCAN = 5.0  # a rescan even without events (a missed one)
COALESCE = 0.01  # a burst of events, one scan
MOVE_RETRY = 1.0  # before asking again to move the same stream
DRAIN = 1.5  # on SIGTERM, for the app's streams to close first

ENV = {**os.environ, "LC_ALL": "C"}


def _die_with_parent(sig: int) -> None:
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, sig, 0, 0, 0)


def _child_dies_with_us() -> None:
    _die_with_parent(signal.SIGKILL)


def landing_name(sink: str) -> str:
    return f"{sink}_in"


def node_spec(sink: str, description: str, media_class: str = SINK_CLASS) -> str:
    """The pw-cli ``create-node`` argument for a sink."""
    description = description.replace('"', "").replace("\\", "")
    return (
        "{ factory.name=support.null-audio-sink "
        f'node.name={sink} node.description="{description}" '
        f"media.class={media_class} audio.position=[ FL FR ] "
        "node.driver=true node.virtual=true "
        "monitor.channel-volumes=true monitor.passthrough=true "
        "object.linger=false state.restore-props=false state.restore-target=false }"
    )


def graph() -> list[dict]:
    """Every object PipeWire shows (``pw-dump``), or nothing."""
    try:
        out = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5, env=ENV, check=False
        ).stdout
        data = json.loads(out or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return [o for o in data if isinstance(o, dict)] if isinstance(data, list) else []


def _props(obj: dict) -> dict:
    info = obj.get("info")
    props = info.get("props") if isinstance(info, dict) else None
    return props if isinstance(props, dict) else {}


def find_sink(
    objects: list[dict], sink: str, media_class: str = SINK_CLASS
) -> int | None:
    for obj in objects:
        props = _props(obj)
        node_id = obj.get("id")
        if (
            obj.get("type") == "PipeWire:Interface:Node"
            and isinstance(node_id, int)
            and props.get("node.name") == sink
            and props.get("media.class") == media_class
        ):
            return node_id
    return None


def sinks_up(objects: list[dict], sink: str) -> bool:
    return (
        find_sink(objects, sink) is not None
        and find_sink(objects, landing_name(sink), LANDING_CLASS) is not None
    )


def _stat_fields(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/stat") as handle:
            stat = handle.read()
    except OSError:
        return None
    return stat[stat.rfind(")") + 2 :].split() or None


def belongs(pid: int, supervisor: int, group: int) -> bool:
    """``pid`` is the app's: in its process group, or descended from its
    supervisor (a process that left the group with setsid is still a
    descendant, or was adopted by the supervisor)."""
    for _ in range(64):
        if pid == supervisor:
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


class Mover:
    def __init__(self, sink: str, supervisor: int, group: int) -> None:
        self.sink = sink
        self.supervisor = supervisor
        self.group = group
        self.asked: dict[int, float] = {}  # stream node id -> last move request

    def app_streams(self, objects: list[dict]) -> tuple[int | None, dict[int, set]]:
        """The sink's id, and the app's playback streams with the nodes each
        is linked to."""
        target = find_sink(objects, self.sink)
        linked: dict[int, set[int]] = {}
        for obj in objects:
            if obj.get("type") == "PipeWire:Interface:Link":
                info = obj.get("info") or {}
                out, into = info.get("output-node-id"), info.get("input-node-id")
                if isinstance(out, int) and isinstance(into, int):
                    linked.setdefault(out, set()).add(into)
        streams: dict[int, set] = {}
        for obj in objects:
            props = _props(obj)
            node_id = obj.get("id")
            if (
                obj.get("type") != "PipeWire:Interface:Node"
                or not isinstance(node_id, int)
                or props.get("media.class") != "Stream/Output/Audio"
            ):
                continue
            pid = str(props.get("application.process.id", ""))
            if pid.isdigit() and belongs(int(pid), self.supervisor, self.group):
                streams[node_id] = linked.get(node_id, set())
        return target, streams

    def scan(self) -> bool:
        """Move the app's streams that are not on the sink. False once the sink
        is gone: nothing is ever moved anywhere else."""
        target, streams = self.app_streams(graph())
        if target is None:
            return False
        now = time.monotonic()
        for node_id, targets in streams.items():
            if (
                target in targets
                or now - self.asked.get(node_id, -MOVE_RETRY) < MOVE_RETRY
            ):
                continue
            self.asked[node_id] = now
            subprocess.run(
                [
                    "pw-metadata",
                    "-n",
                    "default",
                    str(node_id),
                    "target.object",
                    self.sink,
                ],
                capture_output=True,
                timeout=5,
                env=ENV,
                check=False,
            )
        self.asked = {k: v for k, v in self.asked.items() if k in streams}
        return True

    def drain(self) -> None:
        """Wait (bounded) until the app has no playback stream left."""
        deadline = time.monotonic() + DRAIN
        while time.monotonic() < deadline:
            _target, streams = self.app_streams(graph())
            if not streams:
                return
            time.sleep(0.1)


def _subscribe() -> subprocess.Popen | None:
    try:
        return subprocess.Popen(
            ["pactl", "subscribe"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=ENV,
            preexec_fn=_child_dies_with_us,
        )
    except OSError:
        return None


def _end(proc: subprocess.Popen | None) -> None:
    # Only this process reaps its children (it is no subreaper): a child that
    # exited stays a zombie until wait(), so its PID is never someone else's.
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait()


class Stop(Exception):
    pass


def _on_term(_signum, _frame):
    raise Stop


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: sound.py SINK DESCRIPTION", file=sys.stderr)
        return 2
    sink, description = sys.argv[1], sys.argv[2]
    _die_with_parent(signal.SIGTERM)
    supervisor, group = os.getppid(), os.getpgid(0)
    if supervisor == 1:
        return 1  # the supervisor is already gone
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    holder = subscriber = None
    mover = Mover(sink, supervisor, group)
    try:
        holder = subprocess.Popen(
            ["pw-cli"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=ENV,
            preexec_fn=_child_dies_with_us,
        )
        assert holder.stdin is not None
        landing = node_spec(landing_name(sink), description, LANDING_CLASS)
        holder.stdin.write(
            f"create-node adapter {node_spec(sink, description)}\n"
            f"create-node adapter {landing}\n".encode()
        )
        holder.stdin.flush()
        deadline = time.monotonic() + NODE_TIMEOUT
        while not sinks_up(graph(), sink):
            if time.monotonic() > deadline or holder.poll() is not None:
                print("failed", flush=True)
                return 1
            time.sleep(0.05)
        print("ok", flush=True)

        dirty, last_scan = True, 0.0
        while holder.poll() is None:
            if subscriber is None or subscriber.poll() is not None:
                _end(subscriber)
                subscriber = _subscribe()
                dirty = True
            if dirty:
                time.sleep(COALESCE)
                if not mover.scan():
                    return 0  # the sink is gone
                dirty, last_scan = False, time.monotonic()
            wait = max(0.0, SAFETY_SCAN - (time.monotonic() - last_scan))
            if subscriber is None or subscriber.stdout is None:
                time.sleep(min(wait, 1.0))
                dirty = time.monotonic() - last_scan >= SAFETY_SCAN
                continue
            ready, _, _ = select.select([subscriber.stdout], [], [], wait)
            if not ready:
                dirty = True  # the safety rescan
                continue
            chunk = os.read(subscriber.stdout.fileno(), 65536)
            if not chunk:
                _end(subscriber)
                subscriber = None
                time.sleep(1.0)  # the sound server went away: retry
            elif b"sink-input" in chunk:
                dirty = True
        return 0
    except Stop:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        mover.drain()
        return 0
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _end(subscriber)
        _end(holder)


if __name__ == "__main__":
    sys.exit(main())
