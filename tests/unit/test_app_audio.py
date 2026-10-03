"""Per-app sound against the user's real PipeWire. Skipped without it.

Every sink these tests make belongs to an app they launch (or to a holder
they own) and disappears with it; the only module loaded is a decoy null sink
that a test unloads itself.
"""

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from app import sessions, sound
from test_app_sessions import PROBE, _launch, _probe_env
from test_app_sessions import pytestmark as needs_x11

needs_pipewire = pytest.mark.skipif(
    not sessions.audio_available(), reason="needs PipeWire and its tools"
)
needs_gst = pytest.mark.skipif(
    shutil.which("gst-launch-1.0") is None, reason="needs gst-launch-1.0"
)

TONE = "exec gst-launch-1.0 -q audiotestsrc freq=440 is-live=true ! audioconvert ! pulsesink"
# The streamer's capture (app/streamer.py CAPTURE_PROPS, minus the name).
CAPTURE = (
    "props,media.type=Audio,media.category=Capture,"
    "stream.capture.sink=(boolean)true,node.dont-fallback=(boolean)true,"
    "node.dont-reconnect=(boolean)true,node.dont-move=(boolean)true"
)
WP_STATE = Path.home() / ".local/state/wireplumber/stream-properties"


@pytest.fixture(autouse=True)
def _cleanup(monkeypatch, tmp_path):
    monkeypatch.setenv("X_PROBE_LOG", str(tmp_path / "probe.log"))
    monkeypatch.setenv("X_PROBE_ENV", str(tmp_path / "probe.env"))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    yield
    for record in sessions.list_sessions():
        sessions.stop(record["id"])


def _record(session_id: str) -> dict:
    record = sessions._read_record(session_id)
    assert record is not None
    return record


def _node(name: str) -> int | None:
    return sound.find_sink(sound.graph(), name)


def _wait(condition, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, message
        time.sleep(0.05)


def _links_from(pid: int) -> set[str]:
    """Names of the nodes the playback streams of process ``pid`` feed."""
    objects = sound.graph()
    names = {
        o["id"]: sound.props(o).get("node.name")
        for o in objects
        if o.get("type") == "PipeWire:Interface:Node"
    }
    streams = {
        o["id"]
        for o in objects
        if o.get("type") == "PipeWire:Interface:Node"
        and sound.props(o).get("media.class") == "Stream/Output/Audio"
        and str(sound.props(o).get("application.process.id")) == str(pid)
    }
    return {
        names.get(o["info"]["input-node-id"])
        for o in objects
        if o.get("type") == "PipeWire:Interface:Link"
        and o["info"]["output-node-id"] in streams
    }


def _pids(*, command: str, group: int | None = None, parent: int | None = None):
    """Processes whose command line starts with ``command``, in ``group`` or
    children of ``parent`` (a process keeps its group when its parent dies)."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2 :].split()
        if not argv.startswith(command):  # the program, not an argument
            continue
        if group is not None and int(fields[2]) != group:
            continue
        if parent is not None and int(fields[1]) != parent:
            continue
        found.append(int(entry.name))
    return found


def _holder(record: dict) -> list[int]:
    """The pw-cli connection holding the app's nodes (the supervisor's child)."""
    return _pids(command="pw-cli", parent=record["app_pid"])


def _tone_pid(record: dict) -> int:
    """The gst-launch process playing the app's tone."""
    deadline = time.monotonic() + 10
    while not (pids := _pids(command="gst-launch-1.0 ", group=record["app_pid"])):
        assert time.monotonic() < deadline, "no tone process"
        time.sleep(0.05)
    return pids[0]


def _gone(*names: str) -> bool:
    objects = sound.graph()
    return all(sound.find_sink(objects, name) is None for name in names)


def _hears(sink: str, timeout: float = 10.0) -> bool:
    """The sink's monitor carries sound within ``timeout`` (a stream that
    just linked may take a moment to start playing)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _level(sink) > -40:
            return True
    return False


def _level(sink: str, seconds: float = 1.5) -> float:
    """Loudest RMS (dB) on ``sink``'s monitor, captured like the streamer."""
    out = subprocess.run(
        [
            "timeout",
            str(seconds),
            "gst-launch-1.0",
            "-m",
            "pipewiresrc",
            f"target-object={sink}",
            f"stream-properties={CAPTURE}",
            "!",
            "audio/x-raw,channels=2",
            "!",
            "audioconvert",
            "!",
            "level",
            "interval=200000000",
            "!",
            "fakesink",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    values = []
    for line in out.splitlines():
        if "rms=(GValueArray)<" in line:
            inner = line.split("rms=(GValueArray)<")[1].split(">")[0]
            values += [float(v) for v in inner.split(",") if v.strip()]
    return max(values) if values else -1000.0


@needs_x11
@needs_pipewire
def test_a_stream_app_gets_its_sink_its_guard_and_routing(tmp_path):
    record = _record(_launch(audio="stream")["id"])
    sink = record["audio_sink"]
    assert record["audio"] == "stream"
    assert sink.startswith(f"{sessions.SINK_PREFIX}probe_")
    assert "audio_module" not in record
    objects = sound.graph()
    assert sound.ready(objects, sink)
    assert sound.ready(objects, sound.guard_name(sink))
    pulse_sinks = subprocess.run(
        ["pactl", "list", "short", "sinks"], capture_output=True, text=True
    ).stdout
    assert sink in pulse_sinks  # pulse clients can play into it
    env = _probe_env(tmp_path)
    assert env["PULSE_SINK"] == sink
    assert env["PIPEWIRE_NODE"] == sink
    assert env["SDL_AUDIO_DRIVER"].startswith("pipewire,")
    assert env["SDL_AUDIODRIVER"].startswith("pipewire,")
    assert sessions.public(record)["audio"] == "stream"


@needs_x11
@needs_pipewire
def test_the_sink_never_outranks_its_guard():
    """WirePlumber picks a default output by priority.session, then by age
    (lower serial). The guard wins both, so when the real outputs go away the
    silent guard becomes the default, never the captured sink."""
    record = _record(_launch(audio="stream")["id"])
    found = {}
    for obj in sound.graph():
        props = sound.props(obj)
        if props.get("node.name") in (
            record["audio_sink"],
            sound.guard_name(record["audio_sink"]),
        ):
            found[props["node.name"]] = (
                int(props.get("priority.session", 0)),
                -int(props["object.serial"]),
            )
    sink, guard = (
        found[record["audio_sink"]],
        found[sound.guard_name(record["audio_sink"])],
    )
    assert guard[0] > sink[0]
    assert guard[1] > sink[1]  # created first


@needs_gst
@needs_x11
@needs_pipewire
def test_the_app_sound_reaches_its_sink_and_nothing_else():
    record = _record(_launch(args=["--tone", "440"])["id"])
    sink = record["audio_sink"]
    tone = _tone_pid(record)
    _wait(lambda: _links_from(tone), 10, "the tone opened no stream")
    assert _links_from(tone) == {sink}
    assert _hears(sink)


@needs_x11
@needs_pipewire
def test_stop_removes_the_sink_and_its_guard():
    record = _record(_launch(audio="stream")["id"])
    sink = record["audio_sink"]
    sessions.stop("probe")
    _wait(lambda: _gone(sink, sound.guard_name(sink)), 5, "a node outlived stop")


@needs_gst
@needs_x11
@needs_pipewire
def test_an_exit_removes_the_sink():
    record = _record(
        sessions.launch(
            ["sh", "-c", f"timeout 0.5 {TONE[5:]}"], name="brief", gpu="off", wait=0
        )["id"]
    )
    _wait(lambda: sessions.get("brief")["status"] == "exited", 15, "never exited")
    sink = record["audio_sink"]
    _wait(lambda: _gone(sink, sound.guard_name(sink)), 5, "a node outlived the app")


@needs_x11
@needs_pipewire
def test_the_nodes_die_with_a_killed_supervisor():
    """Nothing is unloaded by number: the nodes are a connection's objects,
    and the connection dies with the supervisor (parent-death signal)."""
    record = _record(_launch(audio="stream")["id"])
    group = record["app_pid"]
    holder = _holder(record)
    assert holder
    os.kill(group, signal.SIGKILL)
    try:
        sink = record["audio_sink"]
        _wait(lambda: _gone(sink, sound.guard_name(sink)), 5, "a node outlived it")
        _wait(lambda: not Path(f"/proc/{holder[0]}").exists(), 5, "the holder lived on")
    finally:
        try:
            os.killpg(group, signal.SIGKILL)  # the probe, now leaderless
        except ProcessLookupError:
            pass


@needs_gst
@needs_x11
@needs_pipewire
def test_a_stopping_app_never_reaches_the_speakers():
    """The sink stays until every process of the app is gone: an app that
    ignores SIGTERM and keeps playing (killed after the grace) never falls
    back to a real output."""
    record = _record(
        sessions.launch(
            [
                "sh",
                "-c",
                "trap '' TERM; exec gst-launch-1.0 -q audiotestsrc volume=0 "
                "is-live=true ! audioconvert ! pulsesink",
            ],
            name="stubborn",
            gpu="off",
            wait=0,
        )["id"]
    )
    sink = record["audio_sink"]
    tone = _tone_pid(record)
    _wait(lambda: _links_from(tone) == {sink}, 10, "the tone never played")
    seen: set = set()
    stopper = threading.Thread(target=sessions.stop, args=("stubborn",))
    stopper.start()
    while stopper.is_alive() or Path(f"/proc/{tone}").exists():
        seen |= _links_from(tone)
        time.sleep(0.03)
    stopper.join()
    assert seen == {sink}, f"the stopping app played into {seen - {sink}}"
    _wait(lambda: _gone(sink, sound.guard_name(sink)), 5, "a node outlived stop")


@needs_gst
@needs_x11
@needs_pipewire
def test_a_stream_naming_another_output_is_left_alone_and_unheard():
    """Nothing moves streams: one the app opens on a named output (as SDL's
    pulse backend does with the default one) plays there, and the capture
    does not hear it."""
    decoy = f"merlin_test_decoy_{uuid.uuid4().hex[:8]}"
    module = subprocess.run(
        ["pactl", "load-module", "module-null-sink", f"sink_name={decoy}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    try:
        record = _record(_launch(args=["--tone", "440", "--tone-device", decoy])["id"])
        tone = _tone_pid(record)
        _wait(lambda: _links_from(tone) == {decoy}, 10, "the tone never played")
        assert not _hears(record["audio_sink"], timeout=3)
        assert _links_from(tone) == {decoy}
    finally:
        sessions.stop("probe")
        subprocess.run(["pactl", "unload-module", module], check=False)


@needs_gst
@needs_x11
@needs_pipewire
def test_wireplumber_keeps_no_state_for_app_sinks():
    record = _record(_launch(args=["--tone", "440"])["id"])
    time.sleep(1.5)
    sessions.stop("probe")
    time.sleep(1.5)  # WirePlumber saves its state shortly after a change
    if not WP_STATE.exists():
        pytest.skip("no WirePlumber state file")
    state = WP_STATE.read_text()
    assert record["audio_sink"] not in state
    assert sound.guard_name(record["audio_sink"]) not in state


@needs_x11
def test_local_mode_leaves_the_sound_alone(tmp_path):
    record = _record(_launch(audio="local")["id"])
    assert record["audio"] == "local" and record["audio_sink"] is None
    assert not _holder(record)
    env = _probe_env(tmp_path)
    for name in sessions._audio_env("x"):
        assert env.get(name) == os.environ.get(name)  # inherited, untouched


@needs_x11
@needs_pipewire
def test_a_sink_that_cannot_be_made_falls_back_to_local(monkeypatch, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "pw-cli").write_text("#!/bin/sh\nexit 1\n")
    (fake / "pw-cli").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    record = _record(_launch(audio="stream")["id"])
    assert record["audio"] == "local" and record["audio_sink"] is None
    assert record["audio_requested"] == "stream"
    env = _probe_env(tmp_path)
    for name in sessions._audio_env("x"):
        assert env.get(name) == os.environ.get(name)  # the routing was dropped


@needs_x11
def test_no_sound_server_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(sessions, "audio_available", lambda: False)
    record = _record(_launch(audio="stream")["id"])
    assert record["audio"] == "local" and record["audio_sink"] is None
    assert record["audio_requested"] == "stream"


@needs_x11
def test_saved_apps_carry_the_audio_mode(tmp_path):
    saved = sessions.save_saved(
        {"name": "beep", "command": str(PROBE), "cwd": str(tmp_path), "audio": "local"}
    )
    assert saved["audio"] == "local"
    with pytest.raises(ValueError, match="audio"):
        sessions.save_saved(
            {"name": "bad", "command": "true", "cwd": str(tmp_path), "audio": "loud"}
        )
    record = sessions.launch_saved("beep")
    assert record["audio"] == "local"


def test_plain_pulseaudio_does_not_stream(monkeypatch):
    """Plain PulseAudio would move the capture to the microphone when a sink
    goes away: only PipeWire's pulse server qualifies."""

    def info(*args, timeout=5.0):
        return subprocess.CompletedProcess(
            ["pactl", *args], 0, stdout="Server Name: pulseaudio\n", stderr=""
        )

    monkeypatch.setattr(sessions, "_pactl", info)
    assert sessions.audio_available() is False


def test_the_node_specs():
    (guard, guard_spec), (sink, sink_spec) = sound.specs("merlin_app_x_1")
    assert (guard, sink) == ("merlin_app_x_1_guard", "merlin_app_x_1")
    for spec in (guard_spec, sink_spec):
        for needed in (
            "media.class=Audio/Sink",
            "object.linger=false",
            "state.restore-props=false",
            "state.restore-target=false",
        ):
            assert needed in spec
    assert f"priority.session={sound.GUARD_PRIORITY}" in guard_spec
    assert f"priority.session={sound.SINK_PRIORITY}" in sink_spec
    assert sound.GUARD_PRIORITY > sound.SINK_PRIORITY
    assert 'node.description="x"' in sound.node_spec("n", 'x"\\', 0)
