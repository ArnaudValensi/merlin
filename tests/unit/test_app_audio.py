"""Per-app sound against the user's real PipeWire. Skipped without it.

Every sink these tests make belongs to an app they launch (or to a holder
they own) and disappears with it; the only module loaded is a decoy null sink
that a test unloads itself.
"""

import json
import os
import shutil
import signal
import subprocess
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
        o["id"]: sound._props(o).get("node.name")
        for o in objects
        if o.get("type") == "PipeWire:Interface:Node"
    }
    streams = {
        o["id"]
        for o in objects
        if o.get("type") == "PipeWire:Interface:Node"
        and sound._props(o).get("media.class") == "Stream/Output/Audio"
        and str(sound._props(o).get("application.process.id")) == str(pid)
    }
    return {
        names.get(o["info"]["input-node-id"])
        for o in objects
        if o.get("type") == "PipeWire:Interface:Link"
        and o["info"]["output-node-id"] in streams
    }


def _group_pids(group: int, *, command: str) -> list[int]:
    """Processes of ``group`` whose command line contains ``command`` (a
    process keeps its group when its parent dies)."""
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
        if command in argv and int(fields[2]) == group:
            found.append(int(entry.name))
    return found


def _tone_pid(record: dict) -> int:
    """The gst-launch process playing the app's tone."""
    deadline = time.monotonic() + 10
    while not (pids := _group_pids(record["app_pid"], command="audiotestsrc")):
        assert time.monotonic() < deadline, "no tone process"
        time.sleep(0.05)
    return pids[0]


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
def test_a_stream_app_gets_its_own_sink_and_routing(tmp_path):
    record = _record(_launch(audio="stream")["id"])
    sink = record["audio_sink"]
    assert record["audio"] == "stream"
    assert sink.startswith(f"{sessions.SINK_PREFIX}probe_")
    assert "audio_module" not in record
    assert _node(sink) is not None  # an Audio/Sink/Virtual node (find_sink)
    landing = sound.landing_name(sink)
    assert sound.find_sink(sound.graph(), landing, sound.LANDING_CLASS) is not None
    pulse_sinks = subprocess.run(
        ["pactl", "list", "short", "sinks"], capture_output=True, text=True
    ).stdout
    assert landing in pulse_sinks  # where pulse clients can start
    assert sink not in pulse_sinks.replace(landing, "")  # the private one: never
    env = _probe_env(tmp_path)
    assert env["PULSE_SINK"] == landing
    assert env["PIPEWIRE_NODE"] == landing
    assert env["SDL_AUDIO_DRIVER"] == "pulseaudio"
    assert env["SDL_AUDIODRIVER"] == "pulseaudio"
    assert sessions.public(record)["audio"] == "stream"


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
def test_stop_removes_the_sink():
    record = _record(_launch(audio="stream")["id"])
    sessions.stop("probe")
    landing = sound.landing_name(record["audio_sink"])
    _wait(
        lambda: (
            _node(record["audio_sink"]) is None
            and sound.find_sink(sound.graph(), landing, sound.LANDING_CLASS) is None
        ),
        5,
        "a sink outlived stop",
    )


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
    _wait(lambda: _node(record["audio_sink"]) is None, 5, "the sink outlived the app")


@needs_x11
@needs_pipewire
def test_the_sink_dies_with_a_killed_supervisor():
    """No unload by number, no sweep: the sink is a connection's object, and
    every process in the chain dies with its parent."""
    record = _record(_launch(audio="stream")["id"])
    group = record["app_pid"]
    os.kill(group, signal.SIGKILL)
    try:
        _wait(
            lambda: _node(record["audio_sink"]) is None,
            5,
            "the sink outlived its owner",
        )
        _wait(
            lambda: (
                not _group_pids(group, command="sound.py")
                and not _group_pids(group, command="pw-cli")
            ),
            5,
            "the sound helper outlived the supervisor",
        )
    finally:
        try:
            os.killpg(group, signal.SIGKILL)  # the probe, now leaderless
        except ProcessLookupError:
            pass


def _defaults() -> tuple[str, str]:
    def get(what: str) -> str:
        return subprocess.run(
            ["pactl", what], capture_output=True, text=True, check=False
        ).stdout.strip()

    return get("get-default-sink"), get("get-default-source")


@needs_x11
@needs_pipewire
def test_the_sink_is_never_a_default():
    """Even at the highest priority, WirePlumber never makes an app's sink the
    default output or input: other programs' sound never lands in it."""
    name = f"merlin_test_default_{uuid.uuid4().hex[:8]}"
    spec = sound.node_spec(name, "test").replace(
        "object.linger=false", "object.linger=false priority.session=2000000"
    )
    before = _defaults()
    holder = subprocess.Popen(
        ["pw-cli"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
    )
    try:
        assert holder.stdin is not None
        holder.stdin.write(f"create-node adapter {spec}\n".encode())
        holder.stdin.flush()
        _wait(lambda: _node(name) is not None, 5, "no node")
        time.sleep(1.0)  # WirePlumber rescans the defaults on a new node
        assert _defaults() == before
    finally:
        holder.kill()
        holder.wait()
    _wait(lambda: _node(name) is None, 5, "the node outlived its holder")


@needs_gst
@needs_x11
@needs_pipewire
def test_a_stream_opened_on_a_named_device_is_moved_to_the_app():
    """SDL3 opens the default output by name, past PULSE_SINK: sound.py moves
    the app's streams to its sink, and nobody else's; WirePlumber keeps no
    target it could restore onto other programs."""
    decoy = f"merlin_test_decoy_{uuid.uuid4().hex[:8]}"
    module = subprocess.run(
        ["pactl", "load-module", "module-null-sink", f"sink_name={decoy}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    stranger = subprocess.Popen(
        [
            "gst-launch-1.0",
            "-q",
            "audiotestsrc",
            "freq=660",
            "is-live=true",
            "!",
            "pulsesink",
            f"device={decoy}",
        ]
    )
    try:
        record = _record(_launch(args=["--tone", "440", "--tone-device", decoy])["id"])
        sink = record["audio_sink"]
        tone = _tone_pid(record)
        _wait(lambda: _links_from(tone) == {sink}, 10, "the app's stream never moved")
        assert _hears(sink)
        time.sleep(1.0)
        assert _links_from(stranger.pid) == {decoy}, "only the app's streams move"
        if WP_STATE.exists():
            assert sink not in WP_STATE.read_text()
    finally:
        stranger.kill()
        stranger.wait()
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
    assert record["audio_sink"] not in WP_STATE.read_text()


@needs_x11
@needs_pipewire
def test_the_sound_helper_ends_with_the_app():
    record = _record(_launch(audio="stream")["id"])
    group = record["app_pid"]
    assert _group_pids(group, command="sound.py")
    _wait(
        lambda: _group_pids(group, command="pactl subscribe"),
        5,
        "no subscriber started",
    )
    sessions.stop("probe")
    _wait(
        lambda: (
            not _group_pids(group, command="sound.py")
            and not _group_pids(group, command="pw-cli")
            and not _group_pids(group, command="pactl subscribe")
        ),
        10,
        "the sound helper outlived the app",
    )


@needs_x11
def test_local_mode_leaves_the_sound_alone(tmp_path):
    record = _record(_launch(audio="local")["id"])
    assert record["audio"] == "local" and record["audio_sink"] is None
    assert not _group_pids(record["app_pid"], command="sound.py")
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


def test_the_node_spec_keeps_the_sink_private():
    spec = sound.node_spec("merlin_app_x_1", 'Merlin "app" x\\')
    for needed in (
        "node.name=merlin_app_x_1",
        "media.class=Audio/Sink/Virtual",
        "object.linger=false",
        "state.restore-props=false",
        "state.restore-target=false",
        'node.description="Merlin app x"',
    ):
        assert needed in spec
    assert json.loads(json.dumps(spec)) == spec
