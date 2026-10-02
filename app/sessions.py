"""App sessions: one private Xvfb display and one detached app per session.

The single library behind the ``merlin app`` CLI and the ``/api/apps`` routes.
**Standard library only**: the CLI commands run under a plain ``python3``.

A session is a JSON record under ``~/.merlin/data/apps/sessions/<id>.json``.
Its processes are detached (own process groups) so they outlive the CLI call
that started them and any Merlin restart, like tmux. Liveness is checked by
PID *and* process start time, so a recycled PID is never mistaken for the app
and never killed.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paths

SUPERVISOR = Path(__file__).resolve().parent / "supervise.py"
DEFAULT_SIZE = (1280, 720)
FIRST_DISPLAY = 100
LAST_DISPLAY = 999
XVFB_READY_TIMEOUT = 5.0
WINDOW_TIMEOUT = 10.0
TYPE_CHUNK = 24  # characters per locked step of agent typing (~0.3 s)
STOP_GRACE = 10.0  # the supervisor ends leftovers within ~3 s; this bounds a stuck one
CONTROLS = ("gamepad", "trackpad", "touch")
AUDIO_MODES = ("stream", "local")
SINK_PREFIX = "merlin_app_"
GPU_MODES = ("auto", "on", "off")

# Variables that would send an app to the user's real desktop instead of the
# private display, and the overrides that force every toolkit onto X11.
_SCRUB_ENV = ("WAYLAND_DISPLAY", "WAYLAND_SOCKET")
_X11_ENV = {
    "XDG_SESSION_TYPE": "x11",
    "SDL_VIDEODRIVER": "x11",
    "SDL_VIDEO_DRIVER": "x11",
    "GDK_BACKEND": "x11",
    "QT_QPA_PLATFORM": "xcb",
}


_SOFTWARE_GL_ENV = {
    "__GLX_VENDOR_LIBRARY_NAME": "mesa",
    "LIBGL_ALWAYS_SOFTWARE": "1",
}


class AppError(Exception):
    """A runtime failure the CLI reports with exit code 1."""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def apps_dir() -> Path:
    return paths.data_dir() / "data" / "apps"


def _sessions_dir() -> Path:
    return apps_dir() / "sessions"


def log_path(session_id: str) -> Path:
    return apps_dir() / "logs" / f"{session_id}.log"


def _exit_path(session_id: str) -> Path:
    return apps_dir() / "exit" / session_id


def keymap_registry(record: dict) -> Path:
    """The streamer's keycode slots on this record's Xvfb (its identity)."""
    return (
        apps_dir()
        / "keymaps"
        / f"{record.get('xvfb_pid')}-{record.get('xvfb_start')}.json"
    )


def thumb_path(session_id: str) -> Path:
    return apps_dir() / "thumbs" / f"{session_id}.png"


def shots_dir() -> Path:
    return apps_dir() / "shots"


def _record_path(session_id: str) -> Path:
    return _sessions_dir() / f"{session_id}.json"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _read_record(session_id: str) -> dict | None:
    try:
        data = json.loads(_record_path(session_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save_record(record: dict) -> None:
    _write_json(_record_path(record["id"]), record)


_lock_state = threading.local()


@contextmanager
def _launch_lock() -> Iterator[None]:
    """Serialize state changes (launch, stop, exit) across processes.

    Re-entrant within a thread: launch() stops a replaced session while
    holding it. Other threads and processes wait.
    """
    if getattr(_lock_state, "depth", 0):
        _lock_state.depth += 1
        try:
            yield
        finally:
            _lock_state.depth -= 1
        return
    lock = apps_dir() / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _lock_state.depth = 1
        try:
            yield
        finally:
            _lock_state.depth = 0
            fcntl.flock(handle, fcntl.LOCK_UN)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


def _stat_fields(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the command name, None if gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name (field 2) may contain spaces: split after its ')'.
    return stat[stat.rfind(")") + 2 :].split() or None


def _start_time(pid: int) -> int | None:
    """Kernel start time of ``pid`` (clock ticks), None if gone or a zombie."""
    fields = _stat_fields(pid)
    if not fields or fields[0] == "Z":
        return None
    return int(fields[19])


def _identity(pid: int) -> int | None:
    """Start time of ``pid`` even as a zombie (its identity), None if gone."""
    fields = _stat_fields(pid)
    return int(fields[19]) if fields else None


def _reap(pid: int) -> None:
    """Collect ``pid`` if it is our own exited child (the server launched it)."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def process_alive(pid: int | None, start_time: int | None) -> bool:
    if not pid or start_time is None:
        return False
    _reap(pid)
    return _start_time(pid) == start_time


def _group_members(pgid: int) -> list[int]:
    """Live (non-zombie) processes whose process group is ``pgid``."""
    members = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        fields = _stat_fields(int(entry.name))
        if fields and fields[0] != "Z" and int(fields[2]) == pgid:
            members.append(int(entry.name))
    return members


def _owns_group(pid: int, start_time: int) -> bool:
    """Whether process group ``pid`` is provably still the one we started.

    Only when its leader is present (alive or a zombie) with the start time we
    recorded. App groups are led by ``supervise.py``, which outlives every
    process of the app, so nothing of ours is left once the leader is gone;
    a group number seen without its leader may have been reused, and is
    never signalled.
    """
    return _identity(pid) == start_time


def _kill_group(pid: int | None, start_time: int | None) -> None:
    """End the process group we started: SIGTERM, then SIGKILL after a grace.

    For an app the leader is its supervisor: on SIGTERM it ends what is left
    of the app (orphans and setsid escapees included) and exits last, so the
    wait is on the leader. Ownership is re-checked before every signal.
    """
    if not pid or start_time is None:
        return
    _reap(pid)
    for sig, grace in ((signal.SIGTERM, STOP_GRACE), (signal.SIGKILL, 2.0)):
        if not _owns_group(pid, start_time):
            return
        try:
            os.killpg(pid, sig)
        except OSError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            _reap(pid)
            if not process_alive(pid, start_time) and not _group_members(pid):
                return
            time.sleep(0.05)


def _spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen:
    """Start a detached process in its own session (and process group)."""
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Displays
# ---------------------------------------------------------------------------


def _display_paths(number: int) -> tuple[Path, Path]:
    return Path(f"/tmp/.X{number}-lock"), Path(f"/tmp/.X11-unix/X{number}")


def _lock_owner(number: int) -> int | None:
    """PID written in the display's lock file, None if absent or unreadable."""
    try:
        return int(_display_paths(number)[0].read_text().strip())
    except (OSError, ValueError):
        return None


def _display_free(number: int) -> bool:
    lock, sock = _display_paths(number)
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
        except (OSError, ValueError):
            return False
        if _start_time(pid) is not None:
            return False
        # A stale lock left by a crashed server: reclaim it.
        try:
            lock.unlink()
            sock.unlink(missing_ok=True)
        except OSError:
            return False
        return True
    return not sock.exists()


def _start_xvfb(width: int, height: int, log: Path) -> tuple[int, int, int]:
    """Start Xvfb on the first free display. Returns (number, pid, start)."""
    if shutil.which("Xvfb") is None:
        raise AppError(missing_message(["Xvfb"]))
    for number in range(FIRST_DISPLAY, LAST_DISPLAY + 1):
        if not _display_free(number):
            continue
        with log.open("ab") as out:
            proc = _spawn(
                [
                    "Xvfb",
                    f":{number}",
                    "-screen",
                    "0",
                    f"{width}x{height}x24",
                    "-nolisten",
                    "tcp",
                    "-noreset",
                    "+extension",
                    "GLX",
                    "+extension",
                    "RANDR",
                ],
                stdout=out,
                stderr=out,
            )
        sock = _display_paths(number)[1]
        deadline = time.monotonic() + XVFB_READY_TIMEOUT
        lost = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                lost = True  # lost a race for this display: try the next one
                break
            owner = _lock_owner(number)
            if owner is not None and owner != proc.pid:
                # Another X server (another Merlin home, another launcher)
                # took this display between our check and our start.
                lost = True
                break
            if owner == proc.pid and sock.exists():
                start = _start_time(proc.pid)
                if start is not None:
                    return number, proc.pid, start
            time.sleep(0.02)
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if not lost:
            raise AppError(f"Xvfb did not start on :{number} (see {log})")
    raise AppError("No free X display number")


def _xenv(display: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}
    env.update(_X11_ENV)
    env["DISPLAY"] = display
    return env


def _xdotool(display: str, *args: str, timeout: float = 5.0) -> str:
    if shutil.which("xdotool") is None:
        raise AppError(missing_message(["xdotool"]))
    result = subprocess.run(
        ["xdotool", *args],
        capture_output=True,
        text=True,
        env=_xenv(display),
        timeout=timeout,
        check=False,
    )
    return result.stdout


def _visible_windows(display: str) -> set[str]:
    out = _xdotool(display, "search", "--onlyvisible", "--maxdepth", "1", "")
    return set(out.split())


def _app_windows(record: dict) -> list[str]:
    baseline = set(record.get("baseline_windows") or [])
    return sorted(_visible_windows(record["display"]) - baseline, key=int)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40] or "app"


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", value or "")
    if not match:
        raise ValueError(f"size must look like 1280x720, got {value!r}")
    width, height = int(match.group(1)), int(match.group(2))
    if not (64 <= width <= 7680 and 64 <= height <= 4320):
        raise ValueError(f"size out of range: {value}")
    # Video encoders need even dimensions.
    return width - width % 2, height - height % 2


def parse_keys(value: str | None) -> dict[str, str]:
    """``A=x,B=z`` -> {"A": "x", "B": "z"} (gamepad button -> X keysym)."""
    keys: dict[str, str] = {}
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        button, sep, keysym = part.partition("=")
        if not sep or not button.strip() or not keysym.strip():
            raise ValueError(f"key mapping must look like A=x, got {part!r}")
        keys[button.strip()] = keysym.strip()
    return keys


def resolve_gpu(mode: str) -> str:
    """``auto`` -> ``on`` when VirtualGL and a render node exist, else ``off``."""
    if mode not in GPU_MODES:
        raise ValueError(f"gpu must be one of {', '.join(GPU_MODES)}")
    available = shutil.which("vglrun") is not None and any(
        Path("/dev/dri").glob("renderD*")
    )
    if mode == "on" and not available:
        raise AppError(
            "GPU rendering needs VirtualGL (vglrun) and a /dev/dri render node. "
            "Install virtualgl, or use --gpu off."
        )
    if mode == "off":
        return "off"
    return "on" if available else "off"


def tmux_origin() -> dict[str, str]:
    """Where a CLI call came from: the tmux window of ``$TMUX_PANE``, if any."""
    pane = os.environ.get("TMUX_PANE")
    if not pane or shutil.which("tmux") is None:
        return {"kind": "cli"}
    try:
        out = subprocess.run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                pane,
                "#{session_name}\t#{window_id}\t#{window_name}",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {"kind": "cli"}
    parts = out.split("\t")
    if len(parts) != 3 or not parts[1]:
        return {"kind": "cli"}
    return {
        "kind": "terminal",
        "tmux_session": parts[0],
        "tmux_window_id": parts[1],
        "tmux_window_name": parts[2],
    }


def missing_message(tools: list[str]) -> str:
    from app import deps

    return deps.missing_message(tools)


# ---------------------------------------------------------------------------
# Audio: one null sink per app, on PipeWire (through its pulse server)
# ---------------------------------------------------------------------------


def _pactl(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess | None:
    if shutil.which("pactl") is None:
        return None
    try:
        return subprocess.run(
            ["pactl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def audio_available() -> bool:
    """PipeWire's pulse server answers. Plain PulseAudio does not qualify: when
    an app's sink goes away it moves the streamer's capture to the default
    source (the microphone), where PipeWire can be told not to (streamer.py)."""
    result = _pactl("info")
    return bool(result and result.returncode == 0 and "PipeWire" in result.stdout)


def _sinks() -> list[dict]:
    result = _pactl("-f", "json", "list", "sinks")
    if not result or result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    return (
        [sink for sink in data if isinstance(sink, dict)]
        if isinstance(data, list)
        else []
    )


def _home_tag() -> str:
    """This Merlin home's tag in sink names: every home (the live instance, a
    test server) shares the user's sound server, and each one's orphan sweep
    must only ever touch its own sinks."""
    digest = uuid.uuid5(uuid.NAMESPACE_URL, str(apps_dir().resolve())).hex
    return digest[:6]


def _sink_name(session_id: str, generation: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "_", session_id)
    return f"{SINK_PREFIX}{_home_tag()}_{safe}_{generation[:8]}"


def _create_sink(name: str) -> int | None:
    """Load a null sink called ``name``; its module index, or None."""
    result = _pactl(
        "load-module",
        "module-null-sink",
        f"sink_name={name}",
        f"sink_properties=device.description=Merlin-app-{name}",
    )
    if not result or result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _remove_sink(name: str | None, module: int | None) -> bool:
    """Unload ``module``, but only while it still owns a sink named exactly
    ``name``: module indexes are reused after a sound-server restart, and
    Merlin never unloads a module it did not create."""
    if not name or module is None:
        return False
    for sink in _sinks():
        if sink.get("name") == name and str(sink.get("owner_module")) == str(module):
            result = _pactl("unload-module", str(module))
            return bool(result and result.returncode == 0)
    return False


def _audio_env(sink: str) -> dict[str, str]:
    """Point every common audio API at ``sink``. An app that names its
    output anyway (SDL3 names the default one) is moved by its supervisor."""
    return {
        "PULSE_SINK": sink,  # libpulse, PipeWire's pulse server
        "PIPEWIRE_NODE": sink,  # native PipeWire clients, its ALSA plugin
        "SDL_AUDIO_DRIVER": "pulseaudio",  # SDL3: its pulse backend
        "SDL_AUDIODRIVER": "pulseaudio",  # SDL2
    }


def _sweep_sinks(records: list[dict]) -> None:
    """Unload this home's sinks that no live app owns (a crash, a restart)."""
    live = {
        r.get("audio_sink")
        for r in records
        if r.get("status") in ("starting", "running") and r.get("audio_sink")
    }
    mine = f"{SINK_PREFIX}{_home_tag()}_"
    for sink in _sinks():
        name = str(sink.get("name") or "")
        if name.startswith(mine) and name not in live:
            module = str(sink.get("owner_module") or "")
            if module.isdigit():
                _remove_sink(name, int(module))


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _update_record(
    record: dict, changes: dict, *, only_status: tuple[str, ...] | None = None
) -> dict | None:
    """Apply ``changes`` to the stored record of the same launch, atomically.

    Under the state lock, re-reads the record and checks its generation: a
    record deleted meanwhile (stopped) is never recreated, and a replacement
    (``run --replace``) is never touched. Returns the stored record, or None
    when it is gone or no longer the same launch.
    """
    with _launch_lock():
        current = _read_record(record["id"])
        if current is None or current.get("generation") != record.get("generation"):
            return None
        if only_status is not None and current.get("status") not in only_status:
            return current
        current.update(changes)
        _save_record(current)
        return current


def _refresh(record: dict) -> dict | None:
    """Detect an app that exited (or a display that died) and tear down.

    Returns the current record, or None when it is gone (stopped meanwhile).
    """
    status = record.get("status")
    if status not in ("starting", "running"):
        return record
    app_alive = process_alive(record.get("app_pid"), record.get("app_start"))
    xvfb_alive = process_alive(record.get("xvfb_pid"), record.get("xvfb_start"))
    if app_alive and xvfb_alive:
        if status == "starting":
            try:
                if _app_windows(record):
                    updated = _update_record(
                        record, {"status": "running"}, only_status=("starting",)
                    )
                    return updated
            except (AppError, OSError, subprocess.SubprocessError):
                pass
        return record
    with _launch_lock():
        # Re-read under the lock: a concurrent stop kills the app first and
        # deletes the record after; saving "exited" now would resurrect it.
        current = _read_record(record["id"])
        if current is None:
            return None
        if current.get("status") not in ("starting", "running") or (
            current.get("generation") != record.get("generation")
        ):
            return current
        code: int | None = None
        try:
            code = int(_exit_path(record["id"]).read_text().strip())
        except (OSError, ValueError):
            pass
        _kill_group(current.get("app_pid"), current.get("app_start"))
        _kill_group(current.get("xvfb_pid"), current.get("xvfb_start"))
        keymap_registry(current).unlink(missing_ok=True)
        _remove_sink(current.get("audio_sink"), current.get("audio_module"))
        current["status"] = "exited"
        current["exit_code"] = code
        current["exited_at"] = now_iso()
        _save_record(current)
        return current


def get(session_id: str) -> dict:
    record = _read_record(session_id)
    refreshed = _refresh(record) if record is not None else None
    if refreshed is None or refreshed.get("status") == "stopping":
        raise KeyError(session_id)
    return refreshed


def list_sessions() -> list[dict]:
    directory = _sessions_dir()
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        record = _read_record(path.stem)
        refreshed = _refresh(record) if record is not None else None
        if refreshed is not None and refreshed.get("status") != "stopping":
            records.append(refreshed)
    records.sort(key=lambda r: r.get("started_at", ""))
    return records


def sweep() -> None:
    """Refresh every record and drop orphan sinks (server start-up: apps that
    died while Merlin was down). Under the lock: a launch elsewhere creates
    its sink and its record together."""
    with _launch_lock():
        _sweep_sinks(list_sessions())


def launch(
    argv: list[str],
    *,
    name: str | None = None,
    cwd: str | None = None,
    size: tuple[int, int] = DEFAULT_SIZE,
    gpu: str = "auto",
    controls: str | None = None,
    keys: dict[str, str] | None = None,
    fill: bool = True,
    replace: bool = False,
    origin: dict | None = None,
    saved_id: str | None = None,
    wait: float = WINDOW_TIMEOUT,
    audio: str = "stream",
) -> dict:
    """Start ``argv`` on a fresh private display and return its record.

    ``audio="stream"`` gives the app a null sink of its own, captured by the
    streamer (it then plays nowhere else); ``"local"`` lets it play on the
    machine. Without PipeWire, streaming falls back to local.
    """
    if not argv:
        raise ValueError("no command given")
    if controls is not None and controls not in CONTROLS:
        raise ValueError(f"controls must be one of {', '.join(CONTROLS)}")
    if audio not in AUDIO_MODES:
        raise ValueError(f"audio must be one of {', '.join(AUDIO_MODES)}")
    workdir = Path(cwd or os.getcwd()).expanduser().resolve()
    if not workdir.is_dir():
        raise AppError(f"Folder not found: {workdir}")
    gpu_resolved = resolve_gpu(gpu)
    session_id = slugify(name or Path(argv[0]).name)
    width, height = size

    with _launch_lock():
        existing = _read_record(session_id)
        if existing is not None:
            existing = _refresh(existing) or existing
            if existing["status"] != "exited" and not replace:
                raise AppError(
                    f"'{session_id}' is already running. Stop it first, use "
                    "--replace, or pick another --name."
                )
            _stop_record(existing, thumbnail=False)

        log = log_path(session_id)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_bytes(b"")
        exit_file = _exit_path(session_id)
        exit_file.parent.mkdir(parents=True, exist_ok=True)
        exit_file.unlink(missing_ok=True)

        number, xvfb_pid, xvfb_start = _start_xvfb(
            width, height, apps_dir() / "logs" / f"{session_id}.xvfb.log"
        )
        display = f":{number}"
        generation = uuid.uuid4().hex
        sink: str | None = None
        module: int | None = None
        if audio == "stream" and audio_available():
            sink = _sink_name(session_id, generation)
            module = _create_sink(sink)
            if module is None:
                sink = None
        record: dict[str, Any] = {
            "id": session_id,
            # Identifies this launch: guarded updates never touch a replacement.
            "generation": generation,
            "audio": "stream" if sink else "local",
            "audio_requested": audio,
            "audio_sink": sink,
            "audio_module": module,
            "name": name or Path(argv[0]).name,
            "argv": list(argv),
            "cwd": str(workdir),
            "display": display,
            "size": [width, height],
            "gpu": gpu_resolved,
            "controls": controls,
            "keys": keys or {},
            "fill": fill,
            "xvfb_pid": xvfb_pid,
            "xvfb_start": xvfb_start,
            "app_pid": None,
            "app_start": None,
            "status": "starting",
            "exit_code": None,
            "origin": origin or {"kind": "cli"},
            "saved_id": saved_id,
            "started_at": now_iso(),
            "last_agent_input_at": None,
            "baseline_windows": [],
        }
        try:
            record["baseline_windows"] = sorted(_visible_windows(display))
            prefix = ["vglrun", "-d", "egl"] if gpu_resolved == "on" else []
            env = _xenv(display)
            if sink:
                env.update(_audio_env(sink))
            if gpu_resolved == "off":
                # Software for real: a desktop that pins the NVIDIA GLX vendor
                # would otherwise still render on the GPU.
                env.update(_SOFTWARE_GL_ENV)
            with log.open("ab") as out:
                proc = _spawn(
                    [
                        sys.executable,
                        str(SUPERVISOR),
                        str(exit_file),
                        *(["--audio-sink", sink] if sink else []),
                        "--",
                        *prefix,
                        *argv,
                    ],
                    cwd=str(workdir),
                    env=env,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
            record["app_pid"] = proc.pid
            record["app_start"] = _start_time(proc.pid)
            _save_record(record)
        except BaseException:
            _kill_group(xvfb_pid, xvfb_start)
            _remove_sink(sink, module)
            raise

    _wait_for_window(record, wait)
    try:
        return get(session_id)
    except KeyError:
        # Stopped while starting (a concurrent stop): report it, do not fail.
        return {**record, "status": "stopped"}


def _wait_for_window(record: dict, timeout: float) -> None:
    """Wait for the app's first window; fill the display and focus it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(record["app_pid"], record["app_start"]):
            return
        windows = _app_windows(record)
        if windows:
            window = windows[0]
            if record.get("fill", True):
                width, height = record["size"]
                _xdotool(
                    record["display"],
                    "windowmove",
                    window,
                    "0",
                    "0",
                    "windowsize",
                    window,
                    str(width),
                    str(height),
                )
            _xdotool(record["display"], "windowfocus", window)
            _update_record(record, {"status": "running"}, only_status=("starting",))
            return
        time.sleep(0.1)


def capture(record: dict, path: Path) -> Path:
    """Write a PNG of the whole display to ``path``."""
    if shutil.which("import") is None:
        raise AppError(missing_message(["import"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["import", "-display", record["display"], "-window", "root", str(path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0 or not path.is_file():
        raise AppError(f"Screenshot failed: {result.stderr.strip()}")
    return path


def screenshot(session_id: str, path: Path | None = None) -> Path:
    if path is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = shots_dir() / f"{session_id}-{stamp}.png"
    with _on_display(session_id) as record:
        return capture(record, path)


def capture_thumbnail(session_id: str) -> Path | None:
    """Best-effort last-frame thumbnail (on viewer disconnect and on stop)."""
    with _launch_lock():
        record = _read_record(session_id)
        if record is None or record.get("status") not in ("starting", "running"):
            return None
        try:
            return capture(record, thumb_path(session_id))
        except (AppError, OSError, subprocess.SubprocessError):
            return None


def _stop_record(record: dict, *, thumbnail: bool) -> None:
    with _launch_lock():
        if thumbnail:
            capture_thumbnail(record["id"])
        # Readers skip a "stopping" record, and a concurrent exit check sees
        # it is no longer running, so nothing reports the kill as a crash.
        record["status"] = "stopping"
        _save_record(record)
        _kill_group(record.get("app_pid"), record.get("app_start"))
        _kill_group(record.get("xvfb_pid"), record.get("xvfb_start"))
        _record_path(record["id"]).unlink(missing_ok=True)
        _exit_path(record["id"]).unlink(missing_ok=True)
        keymap_registry(record).unlink(missing_ok=True)
        _remove_sink(record.get("audio_sink"), record.get("audio_module"))


def stop(session_id: str) -> dict:
    # Read under the lock: a `run --replace` finishing first must be the
    # session this stop ends, not overwritten by a stale copy.
    with _launch_lock():
        record = _read_record(session_id)
        if record is None:
            raise KeyError(session_id)
        _stop_record(record, thumbnail=True)
    record["status"] = "stopped"
    return record


def read_log(session_id: str, tail: int | None = None) -> str:
    try:
        text = log_path(session_id).read_text(errors="replace")
    except OSError:
        return ""
    if tail is None:
        return text
    lines = text.splitlines(keepends=True)
    return "".join(lines[-tail:]) if tail > 0 else ""


def _require_running(session_id: str) -> dict:
    record = get(session_id)
    if record["status"] not in ("starting", "running"):
        raise AppError(f"'{session_id}' is not running (status: {record['status']})")
    return record


@contextmanager
def _on_display(session_id: str, generation: str | None = None) -> Iterator[dict]:
    """Hold the state lock while acting on a running app's display.

    Screenshots and agent input connect to the display by its number; under
    the lock that stop and launch take, the app cannot be stopped and its
    display handed to another app between the check and the connection.
    Long input is split into short steps, each under the lock with the same
    ``generation`` re-checked, so the lock is never held for long: other apps
    stay usable and a stop gets in between two steps (the input then ends).
    """
    with _launch_lock():
        record = _require_running(session_id)
        if generation is not None and record.get("generation") != generation:
            raise AppError(f"'{session_id}' was relaunched; input stopped")
        yield record


def _note_agent_input(record: dict) -> None:
    _update_record(record, {"last_agent_input_at": now_iso()})


def _focus_app(record: dict) -> None:
    windows = _app_windows(record)
    if windows:
        _xdotool(record["display"], "windowfocus", windows[0])


def send_keys(
    session_id: str,
    keys: list[str],
    *,
    repeat: int = 1,
    delay_ms: int = 50,
    hold_ms: int = 0,
) -> None:
    """Press and release ``keys`` in order, ``repeat`` times.

    ``hold_ms`` keeps each key down that long: games that read the keyboard
    state once per frame miss a press and release that land in one frame.
    One press at a time under the state lock; holds and delays wait outside.
    """
    steps = [key for _ in range(max(1, repeat)) for key in keys]
    generation: str | None = None
    for i, key in enumerate(steps):
        if hold_ms > 0:
            with _on_display(session_id, generation) as record:
                generation = record.get("generation")
                if i == 0:
                    _focus_app(record)
                _xdotool(record["display"], "keydown", key)
            time.sleep(hold_ms / 1000)
            with _on_display(session_id, generation) as record:
                _xdotool(record["display"], "keyup", key)
        else:
            with _on_display(session_id, generation) as record:
                generation = record.get("generation")
                if i == 0:
                    _focus_app(record)
                _xdotool(record["display"], "key", key)
        if delay_ms > 0 and i < len(steps) - 1:
            time.sleep(delay_ms / 1000)
    with _on_display(session_id, generation) as record:
        _note_agent_input(record)


def type_text(session_id: str, text: str) -> None:
    """Type ``text``, ``TYPE_CHUNK`` characters per step under the lock."""
    generation: str | None = None
    for start in range(0, len(text), TYPE_CHUNK):
        with _on_display(session_id, generation) as record:
            if generation is None:
                generation = record.get("generation")
                _focus_app(record)
            chunk = text[start : start + TYPE_CHUNK]
            _xdotool(
                record["display"],
                "type",
                "--delay",
                "12",
                "--",
                chunk,
                timeout=30 + len(chunk) * 0.05,
            )
    with _on_display(session_id, generation) as record:
        _note_agent_input(record)


def move(session_id: str, x: int, y: int) -> None:
    with _on_display(session_id) as record:
        _xdotool(record["display"], "mousemove", str(x), str(y))
        _note_agent_input(record)


def click(session_id: str, x: int, y: int, button: int = 1) -> None:
    with _on_display(session_id) as record:
        _xdotool(record["display"], "mousemove", str(x), str(y), "click", str(button))
        _note_agent_input(record)


def public(record: dict) -> dict:
    """The record as the CLI and the API show it."""
    out = {
        key: record.get(key)
        for key in (
            "id",
            "name",
            "argv",
            "cwd",
            "display",
            "size",
            "gpu",
            "controls",
            "keys",
            "audio",
            "audio_requested",
            "status",
            "exit_code",
            "origin",
            "saved_id",
            "started_at",
            "last_agent_input_at",
        )
    }
    out["pid"] = record.get("app_pid")
    out["url"] = f"/apps/{record['id']}/play"
    return out


# ---------------------------------------------------------------------------
# Saved apps (the Apps page's launchers)
# ---------------------------------------------------------------------------


def _saved_path() -> Path:
    return apps_dir() / "saved.json"


def list_saved() -> list[dict]:
    try:
        data = json.loads(_saved_path().read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return (
        [app for app in data if isinstance(app, dict)] if isinstance(data, list) else []
    )


def get_saved(saved_id: str) -> dict:
    for app in list_saved():
        if app.get("id") == saved_id:
            return app
    raise KeyError(saved_id)


def _clean_saved(data: dict) -> dict:
    """Validate and normalize a saved app; ValueError on bad input."""
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    command = str(data.get("command") or "").strip()
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"command: {exc}") from exc
    if not argv:
        raise ValueError("command is required")
    cwd = str(data.get("cwd") or "").strip() or str(Path.home())
    if not Path(cwd).expanduser().is_dir():
        raise ValueError(f"folder not found: {cwd}")
    controls = data.get("controls") or None
    if controls is not None and controls not in CONTROLS:
        raise ValueError(f"controls must be one of {', '.join(CONTROLS)}")
    gpu = data.get("gpu") or "auto"
    if gpu not in GPU_MODES:
        raise ValueError(f"gpu must be one of {', '.join(GPU_MODES)}")
    keys = data.get("keys") or {}
    if isinstance(keys, str):
        keys = parse_keys(keys)
    if not isinstance(keys, dict):
        raise ValueError("keys must map buttons to keysyms")
    size = str(data.get("size") or "fit").strip()
    if size != "fit":
        width, height = parse_size(size)
        size = f"{width}x{height}"
    audio = data.get("audio") or "stream"
    if audio not in AUDIO_MODES:
        raise ValueError(f"audio must be one of {', '.join(AUDIO_MODES)}")
    return {
        "id": slugify(name),
        "name": name,
        "command": command,
        "cwd": str(Path(cwd).expanduser()),
        "controls": controls,
        "keys": {str(k): str(v) for k, v in keys.items()},
        "gpu": gpu,
        "size": size,
        "audio": audio,
    }


def save_saved(data: dict, saved_id: str | None = None) -> dict:
    """Create (``saved_id`` None) or replace a saved app; returns it."""
    app = _clean_saved(data)
    with _launch_lock():
        apps = list_saved()
        if saved_id is None:
            if any(a.get("id") == app["id"] for a in apps):
                raise ValueError(f"an app named '{app['name']}' already exists")
            apps.append(app)
        else:
            index = next(
                (i for i, a in enumerate(apps) if a.get("id") == saved_id), None
            )
            if index is None:
                raise KeyError(saved_id)
            if app["id"] != saved_id and any(a.get("id") == app["id"] for a in apps):
                raise ValueError(f"an app named '{app['name']}' already exists")
            apps[index] = app
        _write_json(_saved_path(), apps)
    return app


def delete_saved(saved_id: str) -> None:
    with _launch_lock():
        apps = list_saved()
        kept = [a for a in apps if a.get("id") != saved_id]
        if len(kept) == len(apps):
            raise KeyError(saved_id)
        _write_json(_saved_path(), kept)


def launch_saved(saved_id: str, size: tuple[int, int] | None = None) -> dict:
    """Launch a saved app (the Apps page). ``size`` overrides its own."""
    app = get_saved(saved_id)
    if size is None:
        size = (
            parse_size(app["size"])
            if app.get("size") not in (None, "fit")
            else DEFAULT_SIZE
        )
    return launch(
        shlex.split(app["command"]),
        name=app["name"],
        cwd=app["cwd"],
        size=size,
        gpu=app.get("gpu") or "auto",
        controls=app.get("controls"),
        keys=app.get("keys") or {},
        origin={"kind": "dashboard"},
        saved_id=app["id"],
        audio=app.get("audio") or "stream",
    )
