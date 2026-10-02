"""App sessions against real Xvfb displays, driven by the x_probe fixture app.

Skipped when Xvfb, xdotool or the system Python's python-xlib is missing (CI,
other machines). On a machine with the prerequisites they must run.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import sessions
from app.sessions import AppError

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "tests" / "fixtures" / "x_probe.py"
COMMANDS = ROOT / "app" / "commands"


def _has_xlib() -> bool:
    try:
        return (
            subprocess.run(
                ["/usr/bin/python3", "-c", "import Xlib"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not (shutil.which("Xvfb") and shutil.which("xdotool") and _has_xlib()),
    reason="needs Xvfb, xdotool and python-xlib",
)


@pytest.fixture(autouse=True)
def _cleanup(monkeypatch, tmp_path):
    monkeypatch.setenv("X_PROBE_LOG", str(tmp_path / "probe.log"))
    monkeypatch.setenv("X_PROBE_ENV", str(tmp_path / "probe.env"))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    yield
    for record in sessions.list_sessions():
        sessions.stop(record["id"])


def _probe_env(tmp_path) -> dict:
    path = tmp_path / "probe.env"
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return json.loads(path.read_text())


def _launch(**kwargs) -> dict:
    args = kwargs.pop("args", [])
    kwargs.setdefault("name", "probe")
    kwargs.setdefault("gpu", "off")
    return sessions.launch([str(PROBE), *args], **kwargs)


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists() or _zombie(pid):
            return True
        time.sleep(0.05)
    return False


def _zombie(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat[stat.rfind(")") + 2 :].split()[0] == "Z"


def _cli(command: str, *args: str, env: dict | None = None):
    return subprocess.run(
        [str(COMMANDS / f"{command}.py"), *args],
        capture_output=True,
        text=True,
        env=env or os.environ.copy(),
        timeout=60,
        check=False,
    )


class TestParsing:
    def test_slugify(self):
        assert sessions.slugify("Out of Body!") == "out-of-body"
        assert sessions.slugify("OutOfBody") == "outofbody"
        assert sessions.slugify("///") == "app"

    def test_parse_size(self):
        assert sessions.parse_size("1280x720") == (1280, 720)
        assert sessions.parse_size("1281X721") == (1280, 720)
        with pytest.raises(ValueError):
            sessions.parse_size("big")
        with pytest.raises(ValueError):
            sessions.parse_size("10x10")

    def test_parse_keys(self):
        assert sessions.parse_keys("A=x, B=z,Start=Escape") == {
            "A": "x",
            "B": "z",
            "Start": "Escape",
        }
        assert sessions.parse_keys("") == {}
        with pytest.raises(ValueError):
            sessions.parse_keys("A")

    def test_unknown_gpu_mode(self):
        with pytest.raises(ValueError):
            sessions.resolve_gpu("maybe")


class TestLifecycle:
    def test_launch_runs_on_a_private_x11_display(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
        record = _launch()
        assert record["status"] == "running"
        number = int(record["display"].lstrip(":"))
        assert number >= sessions.FIRST_DISPLAY
        assert Path(f"/tmp/.X11-unix/X{number}").exists()
        env = _probe_env(tmp_path)
        assert env["DISPLAY"] == record["display"]
        assert "WAYLAND_DISPLAY" not in env
        assert env["SDL_VIDEODRIVER"] == "x11"
        assert env["QT_QPA_PLATFORM"] == "xcb"
        # gpu off means software rendering even on an NVIDIA-pinned desktop.
        assert env["__GLX_VENDOR_LIBRARY_NAME"] == "mesa"

    def test_list_and_public_view(self):
        _launch()
        [listed] = sessions.list_sessions()
        view = sessions.public(listed)
        assert view["id"] == "probe"
        assert view["url"] == "/apps/probe/play"
        assert view["status"] == "running"
        assert view["pid"]

    def test_exit_is_detected_with_code_and_logs_kept(self):
        record = _launch(args=["--exit-after", "0.5", "--exit-code", "3"], wait=0)
        xvfb_pid = record["xvfb_pid"] if "xvfb_pid" in record else None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            record = sessions.get("probe")
            if record["status"] == "exited":
                break
            time.sleep(0.1)
        assert record["status"] == "exited"
        assert record["exit_code"] == 3
        assert "x_probe exiting" in sessions.read_log("probe")
        if xvfb_pid:
            assert _gone(xvfb_pid)
        assert [r["id"] for r in sessions.list_sessions()] == ["probe"]

    def test_stop_ends_every_process_and_frees_the_display(self):
        record = sessions._read_record(_launch()["id"])
        assert record is not None
        app_pid, xvfb_pid = record["app_pid"], record["xvfb_pid"]
        sock = Path(f"/tmp/.X11-unix/X{record['display'].lstrip(':')}")
        stopped = sessions.stop("probe")
        assert stopped["status"] == "stopped"
        assert _gone(app_pid) and _gone(xvfb_pid)
        deadline = time.monotonic() + 5
        while sock.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not sock.exists()
        assert sessions.list_sessions() == []
        with pytest.raises(KeyError):
            sessions.stop("probe")

    def test_same_id_needs_replace(self):
        first = sessions._read_record(_launch()["id"])
        assert first is not None
        with pytest.raises(AppError, match="already running"):
            _launch()
        second = sessions._read_record(_launch(replace=True)["id"])
        assert second is not None
        assert second["app_pid"] != first["app_pid"]
        assert _gone(first["app_pid"])
        assert len(sessions.list_sessions()) == 1

    def test_exited_id_is_replaced_without_flag(self):
        _launch(args=["--exit-after", "0.2", "--exit-code", "0"], wait=0)
        deadline = time.monotonic() + 10
        while sessions.get("probe")["status"] != "exited":
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert _launch()["status"] == "running"

    def test_missing_folder(self, tmp_path):
        with pytest.raises(AppError, match="Folder not found"):
            _launch(cwd=str(tmp_path / "nope"))

    def test_two_apps_get_two_displays(self):
        a = _launch(name="one")
        b = _launch(name="two")
        assert a["display"] != b["display"]


class TestCli:
    def test_run_survives_the_cli_and_a_fresh_process_sees_it(self):
        result = _cli("run", "--name", "probe", "--gpu", "off", "--", str(PROBE))
        assert result.returncode == 0, result.stderr
        handle = json.loads(result.stdout)
        assert handle["status"] == "running"
        listed = json.loads(_cli("list").stdout)
        assert [r["id"] for r in listed] == ["probe"]
        assert listed[0]["status"] == "running"
        stopped = _cli("stop", "probe")
        assert json.loads(stopped.stdout)["status"] == "stopped"

    def test_run_reports_a_crash_at_start(self):
        result = _cli(
            "run",
            "--name",
            "crash",
            "--gpu",
            "off",
            "--",
            sys.executable,
            "-c",
            "import sys; print('boom'); sys.exit(7)",
        )
        assert result.returncode == 1
        handle = json.loads(result.stdout)
        assert handle["status"] == "exited"
        assert handle["exit_code"] == 7
        assert "boom" in handle["log_tail"]
        assert "boom" in _cli("logs", "crash").stdout

    def test_usage_errors(self):
        assert _cli("run").returncode == 2
        assert _cli("run", "--size", "huge", "--", "true").returncode == 2
        missing = _cli("stop", "nothing")
        assert missing.returncode == 1
        assert "No app named 'nothing'" in missing.stderr


class TestOrigin:
    def test_terminal_origin_from_tmux_pane(self, monkeypatch, tmp_path):
        if shutil.which("tmux") is None:
            pytest.skip("tmux not installed")
        sock = tmp_path / "t.sock"
        base = ["tmux", "-S", str(sock), "-f", "/dev/null"]
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        subprocess.run(
            [*base, "new-session", "-d", "-s", "alpha", "-n", "work"],
            env=env,
            check=True,
        )
        try:
            out = subprocess.run(
                [
                    *base,
                    "display-message",
                    "-p",
                    "-t",
                    "alpha:",
                    "#{pane_id} #{window_id}",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()
            pane, window = out
            monkeypatch.setenv("TMUX", f"{sock},0,0")
            monkeypatch.setenv("TMUX_PANE", pane)
            origin = sessions.tmux_origin()
            assert origin == {
                "kind": "terminal",
                "tmux_session": "alpha",
                "tmux_window_id": window,
                "tmux_window_name": "work",
            }
        finally:
            subprocess.run([*base, "kill-server"], env=env, check=False)

    def test_no_tmux_is_cli(self, monkeypatch):
        monkeypatch.delenv("TMUX_PANE", raising=False)
        assert sessions.tmux_origin() == {"kind": "cli"}
