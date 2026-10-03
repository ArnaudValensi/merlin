"""The PipeWire side of an app's sound: its sink and the guard beside it.

Standard library only (imported by the supervisor and by sessions); needs
PipeWire's tools (pw-cli, pw-dump).

The supervisor creates both nodes through one ``pw-cli`` connection it keeps
open, with ``object.linger=false``: PipeWire destroys them when that
connection closes, so they live exactly as long as the app (see
supervise.py). Nothing is ever loaded or unloaded by number.

- **The sink** (``merlin_app_<id>_<generation>``, class ``Audio/Sink``, so
  PipeWire's pulse server lists it and pulse clients can play into it) is the
  only node the streamer captures. The app reaches it by name only: its
  environment points there (PULSE_SINK, PIPEWIRE_NODE, and SDL on its
  pipewire backend, which honors PIPEWIRE_NODE where its pulse backend names
  the default output). Nothing moves streams into it.
- **The guard** (``<sink>_guard``, ``Audio/Sink``, never captured) keeps the
  sink from ever becoming the desktop's default output: WirePlumber ranks
  default candidates by ``priority.session``, then by age, and the guard is
  created first with a higher priority (0 against -1). If the machine's real outputs go
  away, the guard (silent) becomes the default, never the sink: no other
  program's sound reaches the capture.
- ``state.restore-*`` off on both: WirePlumber keeps no state for them.
"""

from __future__ import annotations

import json
import os
import subprocess

SINK_CLASS = "Audio/Sink"
# The guard only has to outrank its own sink. At 0 it ties with ordinary
# virtual sinks, and WirePlumber then prefers the older one: the user's own.
SINK_PRIORITY = -1
GUARD_PRIORITY = 0

ENV = {**os.environ, "LC_ALL": "C"}


def guard_name(sink: str) -> str:
    return f"{sink}_guard"


def node_spec(name: str, description: str, priority: int) -> str:
    """The pw-cli ``create-node`` argument for a silent sink."""
    description = description.replace('"', "").replace("\\", "")
    return (
        "{ factory.name=support.null-audio-sink "
        f'node.name={name} node.description="{description}" '
        f"media.class={SINK_CLASS} audio.position=[ FL FR ] "
        f"priority.session={priority} priority.driver={priority} "
        "node.driver=true node.virtual=true "
        "monitor.channel-volumes=true monitor.passthrough=true "
        "object.linger=false state.restore-props=false state.restore-target=false }"
    )


def specs(sink: str) -> list[tuple[str, str]]:
    """(name, spec) to create, in order: the guard first."""
    return [
        (
            guard_name(sink),
            node_spec(guard_name(sink), f"{sink} guard", GUARD_PRIORITY),
        ),
        (sink, node_spec(sink, sink, SINK_PRIORITY)),
    ]


def graph(timeout: float = 5.0) -> list[dict]:
    """Every object PipeWire shows (``pw-dump``), or nothing."""
    try:
        out = subprocess.run(
            ["pw-dump"],
            capture_output=True,
            text=True,
            timeout=max(timeout, 0.1),
            env=ENV,
            check=False,
        ).stdout
        data = json.loads(out or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return [o for o in data if isinstance(o, dict)] if isinstance(data, list) else []


def props(obj: dict) -> dict:
    info = obj.get("info")
    found = info.get("props") if isinstance(info, dict) else None
    return found if isinstance(found, dict) else {}


def find_sink(objects: list[dict], name: str) -> int | None:
    """The id of the sink node called ``name``."""
    for obj in objects:
        node_id = obj.get("id")
        found = props(obj)
        if (
            obj.get("type") == "PipeWire:Interface:Node"
            and isinstance(node_id, int)
            and found.get("node.name") == name
            and found.get("media.class") == SINK_CLASS
        ):
            return node_id
    return None


def ready(objects: list[dict], name: str) -> bool:
    """The sink exists and WirePlumber has set it up (it has input ports):
    from then on it is one of WirePlumber's default candidates."""
    node_id = find_sink(objects, name)
    return node_id is not None and any(
        obj.get("type") == "PipeWire:Interface:Port"
        and str(props(obj).get("node.id")) == str(node_id)
        and props(obj).get("port.direction") == "in"
        for obj in objects
    )
