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
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paths

DEFAULT_SIZE = (1280, 720)
FIRST_DISPLAY = 100
LAST_DISPLAY = 999
XVFB_READY_TIMEOUT = 5.0
WINDOW_TIMEOUT = 10.0
STOP_GRACE = 5.0
CONTROLS = ("gamepad", "trackpad", "touch")
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


def thumb_path(session_id: str) -> Path:
    return apps_dir() / "thumbs" / f"{session_id}.png"


def shots_dir() -> Path:
    return apps_dir() / "shots"


def _record_path(session_id: str) -> Path:
    return _sessions_dir() / f"{session_id}.json"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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


@contextmanager
def _launch_lock() -> Iterator[None]:
    """Serialize display allocation and id claims across processes."""
    lock = apps_dir() / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


def _start_time(pid: int) -> int | None:
    """Kernel start time of ``pid`` (clock ticks), None if gone or a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name (field 2) may contain spaces: split after its ')'.
    fields = stat[stat.rfind(")") + 2 :].split()
    if not fields or fields[0] == "Z":
        return None
    return int(fields[19])


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


def _kill_group(pid: int | None, start_time: int | None) -> None:
    """SIGTERM the process group led by ``pid``, then SIGKILL after a grace.

    Only a group whose leader is still the process we recorded is touched.
    """
    if not process_alive(pid, start_time):
        return
    assert pid is not None
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + STOP_GRACE
    while time.monotonic() < deadline:
        if not process_alive(pid, start_time):
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    _reap(pid)


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
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # lost a race for this display: try the next one
            if sock.exists():
                start = _start_time(proc.pid)
                if start is not None:
                    return number, proc.pid, start
            time.sleep(0.02)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
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
# Lifecycle
# ---------------------------------------------------------------------------


def _refresh(record: dict) -> dict:
    """Detect an app that exited (or a display that died) and tear down."""
    status = record.get("status")
    if status not in ("starting", "running"):
        return record
    app_alive = process_alive(record.get("app_pid"), record.get("app_start"))
    xvfb_alive = process_alive(record.get("xvfb_pid"), record.get("xvfb_start"))
    if app_alive and xvfb_alive:
        if status == "starting":
            try:
                if _app_windows(record):
                    record["status"] = "running"
                    _save_record(record)
            except (AppError, OSError, subprocess.SubprocessError):
                pass
        return record
    code: int | None = None
    try:
        code = int(_exit_path(record["id"]).read_text().strip())
    except (OSError, ValueError):
        pass
    _kill_group(record.get("app_pid"), record.get("app_start"))
    _kill_group(record.get("xvfb_pid"), record.get("xvfb_start"))
    record["status"] = "exited"
    record["exit_code"] = code
    record["exited_at"] = now_iso()
    _save_record(record)
    return record


def get(session_id: str) -> dict:
    record = _read_record(session_id)
    if record is None:
        raise KeyError(session_id)
    return _refresh(record)


def list_sessions() -> list[dict]:
    directory = _sessions_dir()
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        record = _read_record(path.stem)
        if record is not None:
            records.append(_refresh(record))
    records.sort(key=lambda r: r.get("started_at", ""))
    return records


def sweep() -> None:
    """Refresh every record (server start-up: apps that died while down)."""
    list_sessions()


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
) -> dict:
    """Start ``argv`` on a fresh private display and return its record."""
    if not argv:
        raise ValueError("no command given")
    if controls is not None and controls not in CONTROLS:
        raise ValueError(f"controls must be one of {', '.join(CONTROLS)}")
    workdir = Path(cwd or os.getcwd()).expanduser().resolve()
    if not workdir.is_dir():
        raise AppError(f"Folder not found: {workdir}")
    gpu_resolved = resolve_gpu(gpu)
    session_id = slugify(name or Path(argv[0]).name)
    width, height = size

    with _launch_lock():
        existing = _read_record(session_id)
        if existing is not None:
            existing = _refresh(existing)
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
        record: dict[str, Any] = {
            "id": session_id,
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
            wrapper = 'trap "" HUP; "$@"; echo $? > "$MERLIN_APP_EXIT_FILE"'
            env = _xenv(display)
            if gpu_resolved == "off":
                # Software for real: a desktop that pins the NVIDIA GLX vendor
                # would otherwise still render on the GPU.
                env.update(_SOFTWARE_GL_ENV)
            env["MERLIN_APP_EXIT_FILE"] = str(exit_file)
            with log.open("ab") as out:
                proc = _spawn(
                    ["sh", "-c", wrapper, "sh", *prefix, *argv],
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
            raise

    _wait_for_window(record, wait)
    return get(session_id)


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
            current = _read_record(record["id"]) or record
            current["status"] = "running"
            _save_record(current)
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
    record = _require_running(session_id)
    if path is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = shots_dir() / f"{session_id}-{stamp}.png"
    return capture(record, path)


def capture_thumbnail(session_id: str) -> Path | None:
    """Best-effort last-frame thumbnail (on viewer disconnect and on stop)."""
    record = _read_record(session_id)
    if record is None or record.get("status") not in ("starting", "running"):
        return None
    try:
        return capture(record, thumb_path(session_id))
    except (AppError, OSError, subprocess.SubprocessError):
        return None


def _stop_record(record: dict, *, thumbnail: bool) -> None:
    if thumbnail:
        capture_thumbnail(record["id"])
    _kill_group(record.get("app_pid"), record.get("app_start"))
    _kill_group(record.get("xvfb_pid"), record.get("xvfb_start"))
    _record_path(record["id"]).unlink(missing_ok=True)
    _exit_path(record["id"]).unlink(missing_ok=True)


def stop(session_id: str) -> dict:
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


def _note_agent_input(record: dict) -> None:
    current = _read_record(record["id"])
    if current is not None:
        current["last_agent_input_at"] = now_iso()
        _save_record(current)


def _focus_app(record: dict) -> None:
    windows = _app_windows(record)
    if windows:
        _xdotool(record["display"], "windowfocus", windows[0])


def send_keys(
    session_id: str, keys: list[str], *, repeat: int = 1, delay_ms: int = 50
) -> None:
    record = _require_running(session_id)
    _focus_app(record)
    _xdotool(
        record["display"],
        "key",
        "--repeat",
        str(max(1, repeat)),
        "--delay",
        str(max(0, delay_ms)),
        *keys,
        timeout=30 + repeat * len(keys) * max(delay_ms, 1) / 1000,
    )
    _note_agent_input(record)


def type_text(session_id: str, text: str) -> None:
    record = _require_running(session_id)
    _focus_app(record)
    _xdotool(
        record["display"],
        "type",
        "--delay",
        "12",
        "--",
        text,
        timeout=30 + len(text) * 0.05,
    )
    _note_agent_input(record)


def move(session_id: str, x: int, y: int) -> None:
    record = _require_running(session_id)
    _xdotool(record["display"], "mousemove", str(x), str(y))
    _note_agent_input(record)


def click(session_id: str, x: int, y: int, button: int = 1) -> None:
    record = _require_running(session_id)
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
