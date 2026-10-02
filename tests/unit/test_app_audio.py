"""Per-app audio sinks against the user's real sound server (PipeWire or
PulseAudio, through pactl). Skipped without one. Every sink these tests
create is named under the test home's tag and removed again."""

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from app import sessions
from test_app_sessions import PROBE, _launch, _probe_env
from test_app_sessions import pytestmark as needs_x11

pytestmark = [
    needs_x11,
    pytest.mark.skipif(
        not sessions.audio_available(), reason="needs a PipeWire or PulseAudio server"
    ),
]
needs_gst = pytest.mark.skipif(
    shutil.which("gst-launch-1.0") is None, reason="needs gst-launch-1.0"
)

TONE = "exec gst-launch-1.0 -q audiotestsrc freq=440 is-live=true ! audioconvert ! pulsesink"


@pytest.fixture(autouse=True)
def _cleanup(monkeypatch, tmp_path):
    monkeypatch.setenv("X_PROBE_LOG", str(tmp_path / "probe.log"))
    monkeypatch.setenv("X_PROBE_ENV", str(tmp_path / "probe.env"))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    yield
    for record in sessions.list_sessions():
        sessions.stop(record["id"])


def _sink(name: str) -> dict | None:
    return next((s for s in sessions._sinks() if s.get("name") == name), None)


def _sink_inputs() -> list[dict]:
    out = subprocess.run(
        ["pactl", "-f", "json", "list", "sink-inputs"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out or "[]")


def _pids(record: dict) -> set[str]:
    """The app's process group (the supervisor and everything under it)."""
    return {str(pid) for pid in sessions._group_members(record["app_pid"])}


def _tone(name: str = "tone", seconds: float | None = None) -> dict:
    command = TONE if seconds is None else f"timeout {seconds} {TONE[5:]}"
    return sessions.launch(["sh", "-c", command], name=name, gpu="off", wait=0)


def _level(sink: str, seconds: float = 1.5) -> float:
    """Loudest RMS (dB) heard on ``sink``'s monitor over ``seconds``."""
    out = subprocess.run(
        [
            "timeout",
            str(seconds),
            "gst-launch-1.0",
            "-m",
            "pulsesrc",
            f"device={sink}.monitor",
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


def test_a_stream_app_gets_its_own_sink_and_routing(tmp_path):
    record = sessions._read_record(_launch(audio="stream")["id"])
    assert record is not None
    sink, module = record["audio_sink"], record["audio_module"]
    assert record["audio"] == "stream"
    assert sink.startswith(f"{sessions.SINK_PREFIX}{sessions._home_tag()}_probe_")
    found = _sink(sink)
    assert found is not None and str(found["owner_module"]) == str(module)
    env = _probe_env(tmp_path)
    assert env["PULSE_SINK"] == sink
    assert env["PIPEWIRE_NODE"] == sink
    assert env["SDL_AUDIO_DRIVER"] == "pulseaudio"
    assert env["SDL_AUDIODRIVER"] == "pulseaudio"
    assert sessions.public(record)["audio"] == "stream"


@needs_gst
def test_the_app_sound_reaches_its_sink():
    record = sessions._read_record(_tone()["id"])
    assert record is not None
    deadline = time.monotonic() + 10
    loudest = -1000.0
    while time.monotonic() < deadline and loudest < -40:
        loudest = _level(record["audio_sink"])
    assert loudest > -40, f"no tone on the app's sink ({loudest} dB)"
    # Every stream the app opened plays into its own sink, none into the
    # machine's default output.
    sink = _sink(record["audio_sink"])
    assert sink is not None
    pids = _pids(record)
    inputs = [
        i
        for i in _sink_inputs()
        if str(i.get("properties", {}).get("application.process.id")) in pids
    ]
    assert inputs, "the tone opened no stream"
    assert all(i.get("sink") == sink["index"] for i in inputs)


def test_stop_removes_the_sink():
    record = sessions._read_record(_launch(audio="stream")["id"])
    assert record is not None
    sessions.stop("probe")
    assert _sink(record["audio_sink"]) is None


@needs_gst
def test_an_exit_removes_the_sink():
    record = sessions._read_record(_tone("brief", seconds=0.5)["id"])
    assert record is not None
    deadline = time.monotonic() + 15
    while sessions.get("brief")["status"] != "exited":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert _sink(record["audio_sink"]) is None


def test_a_module_that_is_not_ours_is_never_unloaded():
    name = f"merlin_test_foreign_{uuid.uuid4().hex[:8]}"
    module = sessions._create_sink(name)
    assert module is not None
    try:
        assert sessions._remove_sink(name, module + 1) is False  # wrong owner
        assert sessions._remove_sink("merlin_app_not_it", module) is False  # wrong name
        assert _sink(name) is not None
    finally:
        subprocess.run(["pactl", "unload-module", str(module)], check=False)


def test_the_sweep_removes_only_this_homes_orphans():
    orphan = sessions._sink_name("ghost", uuid.uuid4().hex)
    other_home = f"{sessions.SINK_PREFIX}zzzzzz_ghost_{uuid.uuid4().hex[:8]}"
    orphan_module = sessions._create_sink(orphan)
    other_module = sessions._create_sink(other_home)
    assert orphan_module is not None and other_module is not None
    try:
        record = sessions._read_record(_launch(audio="stream")["id"])
        assert record is not None
        sessions.sweep()
        assert _sink(orphan) is None  # ours, no app: removed
        assert _sink(other_home) is not None  # another Merlin home's: kept
        assert _sink(record["audio_sink"]) is not None  # a live app's: kept
    finally:
        subprocess.run(["pactl", "unload-module", str(other_module)], check=False)
        sessions._remove_sink(orphan, orphan_module)


def test_local_mode_leaves_the_sound_alone(tmp_path):
    record = sessions._read_record(_launch(audio="local")["id"])
    assert record is not None
    assert record["audio"] == "local" and record["audio_sink"] is None
    env = _probe_env(tmp_path)
    for name in sessions._audio_env("x"):
        assert env.get(name) == os.environ.get(name)  # inherited, untouched


def test_no_sound_server_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(sessions, "audio_available", lambda: False)
    record = sessions._read_record(_launch(audio="stream")["id"])
    assert record is not None
    assert record["audio"] == "local" and record["audio_sink"] is None
    assert record["audio_requested"] == "stream"


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


def _on_sink(sink: str) -> list[dict]:
    """The playback streams currently on ``sink``."""
    found = _sink(sink)
    return [i for i in _sink_inputs() if found and i.get("sink") == found["index"]]


@needs_gst
def test_a_stream_opened_on_a_named_device_is_moved_to_the_app():
    """SDL3 opens the default output by name, past PULSE_SINK: the supervisor
    moves the app's streams to its sink, and nobody else's."""
    decoy = f"merlin_test_decoy_{uuid.uuid4().hex[:8]}"
    module = sessions._create_sink(decoy)
    assert module is not None
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
        record = sessions._read_record(
            _launch(args=["--tone", "440", "--tone-device", decoy])["id"]
        )
        assert record is not None
        deadline = time.monotonic() + 10
        while not _on_sink(record["audio_sink"]):
            assert time.monotonic() < deadline, "the app's stream was never moved"
            time.sleep(0.1)
        assert _level(record["audio_sink"]) > -40
        on_decoy = {
            i.get("properties", {}).get("application.process.id")
            for i in _on_sink(decoy)
        }
        assert on_decoy == {str(stranger.pid)}, "only the app's streams move"
    finally:
        stranger.kill()
        stranger.wait()
        subprocess.run(["pactl", "unload-module", str(module)], check=False)


def _subscribers(group: int) -> list[int]:
    """The ``pactl subscribe`` processes in the app's process group (a process
    keeps its group when its parent dies)."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")[:2]
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2 :].split()
        if argv == [b"pactl", b"subscribe"] and int(fields[2]) == group:
            found.append(int(entry.name))
    return found


def test_the_mover_ends_with_the_app():
    record = sessions._read_record(_launch(audio="stream")["id"])
    assert record is not None
    group = record["app_pid"]
    deadline = time.monotonic() + 5
    while not _subscribers(group):
        assert time.monotonic() < deadline, "no mover started"
        time.sleep(0.05)
    sessions.stop("probe")
    deadline = time.monotonic() + 10
    while _subscribers(group):
        assert time.monotonic() < deadline, "the mover outlived the app"
        time.sleep(0.05)


def test_a_local_app_has_no_mover():
    record = sessions._read_record(_launch(audio="local")["id"])
    assert record is not None
    time.sleep(1)
    assert not _subscribers(record["app_pid"])
