"""App sessions against real Xvfb displays, driven by the x_probe fixture app.

Skipped when Xvfb, xdotool or the system Python's python-xlib is missing (CI,
other machines). On a machine with the prerequisites they must run.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
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

    def test_stop_racing_an_exit_check_never_resurrects(self):
        for _ in range(3):
            _launch()
            done = threading.Event()

            def poll():
                while not done.is_set():
                    sessions.list_sessions()

            poller = threading.Thread(target=poll)
            poller.start()
            try:
                sessions.stop("probe")
                time.sleep(0.3)
            finally:
                done.set()
                poller.join()
            assert sessions.list_sessions() == []
            assert sessions._read_record("probe") is None

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
        env["SHELL"] = "/bin/sh"  # not the login shell: see test_board_tmux.py
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


IGNORES_TERM = """
import subprocess, sys, time
subprocess.Popen([sys.executable, '-c',
    'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
    'print("child up", flush=True); time.sleep(600)'])
time.sleep(600)
"""

ESCAPES_THE_GROUP = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import os, time; time.sleep(600)'],
                         start_new_session=True)
print("escaped", child.pid, flush=True)
time.sleep(600)
"""

LEAVES_A_CHILD = """
import subprocess, sys
subprocess.Popen([sys.executable, '-c',
    'import time; print("orphan up", flush=True); time.sleep(600)'])
"""


def _wait_log(session_id: str, text: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while text not in sessions.read_log(session_id):
        assert time.monotonic() < deadline, sessions.read_log(session_id)
        time.sleep(0.05)


def _wait_group_empty(pgid: int, timeout: float = 15) -> list[int]:
    deadline = time.monotonic() + timeout
    while sessions._group_members(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return sessions._group_members(pgid)


class TestProcessTree:
    def test_stop_kills_a_child_that_ignores_sigterm(self, monkeypatch):
        monkeypatch.setattr(sessions, "STOP_GRACE", 1.0)
        sessions.launch(
            [sys.executable, "-c", IGNORES_TERM], name="stubborn", gpu="off", wait=0
        )
        _wait_log("stubborn", "child up")
        record = sessions._read_record("stubborn")
        assert record is not None
        pgid = record["app_pid"]
        assert len(sessions._group_members(pgid)) >= 2  # wrapper, parent, child
        sessions.stop("stubborn")
        assert _wait_group_empty(pgid) == []

    def test_exit_cleanup_kills_what_the_dead_wrapper_left(self):
        sessions.launch(
            [sys.executable, "-c", LEAVES_A_CHILD], name="leaver", gpu="off", wait=0
        )
        _wait_log("leaver", "orphan up")
        record = sessions._read_record("leaver")
        assert record is not None
        pgid = record["app_pid"]
        deadline = time.monotonic() + 10
        while sessions.get("leaver")["status"] != "exited":
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert _wait_group_empty(pgid) == []

    def test_an_escaped_setsid_child_is_ended_too(self):
        sessions.launch(
            [sys.executable, "-c", ESCAPES_THE_GROUP], name="escaper", gpu="off", wait=0
        )
        _wait_log("escaper", "escaped ")
        escaped = int(sessions.read_log("escaper").split("escaped ")[1].split()[0])
        assert sessions._start_time(escaped) is not None
        sessions.stop("escaper")
        deadline = time.monotonic() + 15
        while sessions._start_time(escaped) is not None:
            assert time.monotonic() < deadline, "the setsid child survived the stop"
            time.sleep(0.05)

    def test_ownership_needs_the_recorded_leader(self):
        me = os.getpid()
        start = sessions._identity(me)
        assert start is not None
        assert sessions._owns_group(me, start)
        # A live leader with another start time: the number was reused.
        assert not sessions._owns_group(me, start - 1)
        sessions._kill_group(me, start - 1)  # a no-op, or this test dies

    def test_a_zombie_leader_keeps_its_identity(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        deadline = time.monotonic() + 5
        while sessions._start_time(child.pid) is not None:  # until it is a zombie
            assert time.monotonic() < deadline
            time.sleep(0.02)
        start = sessions._identity(child.pid)
        assert start is not None
        assert sessions._owns_group(child.pid, start)
        assert not sessions._owns_group(child.pid, start + 1)  # a reused zombie
        child.wait()
        # Gone: no leader, no proof of ownership, never signalled.
        assert not sessions._owns_group(child.pid, start)


class TestStateRaces:
    def test_stop_waiting_on_a_replace_stops_the_replacement(self):
        first = sessions._read_record(_launch()["id"])
        assert first is not None
        locked = threading.Event()
        stop_called = threading.Event()
        replaced: dict = {}

        def replace_while_holding_the_lock():
            with sessions._launch_lock():
                locked.set()
                stop_called.wait(5)
                time.sleep(0.3)  # let stop read what it reads
                replaced.update(
                    sessions._read_record(_launch(replace=True, wait=0)["id"]) or {}
                )

        worker = threading.Thread(target=replace_while_holding_the_lock)
        worker.start()
        assert locked.wait(5)
        stop_called.set()
        sessions.stop("probe")
        worker.join()
        assert replaced["generation"] != first["generation"]
        assert sessions._read_record("probe") is None
        assert _gone(replaced["app_pid"]) and _gone(replaced["xvfb_pid"])
        assert _gone(first["app_pid"])

    def test_updates_never_recreate_a_stopped_record(self):
        record = sessions._read_record(_launch()["id"])
        assert record is not None
        sessions.stop("probe")
        assert sessions._update_record(record, {"status": "running"}) is None
        sessions._note_agent_input(record)
        assert sessions._read_record("probe") is None

    def test_updates_never_touch_a_replacement(self):
        first = sessions._read_record(_launch()["id"])
        assert first is not None
        _launch(replace=True)
        assert sessions._update_record(first, {"last_agent_input_at": "x"}) is None
        current = sessions._read_record("probe")
        assert current is not None
        assert current["last_agent_input_at"] is None


class TestDisplays:
    def test_a_display_taken_by_another_server_is_not_adopted(self, monkeypatch):
        number = next(
            n
            for n in range(sessions.FIRST_DISPLAY, sessions.LAST_DISPLAY)
            if sessions._display_free(n)
        )
        other = subprocess.Popen(
            ["Xvfb", f":{number}", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            sock = Path(f"/tmp/.X11-unix/X{number}")
            deadline = time.monotonic() + 5
            while not sock.exists():
                assert time.monotonic() < deadline
                time.sleep(0.02)
            # Both "see" it free: the race between two registries.
            real_free = sessions._display_free
            monkeypatch.setattr(
                sessions, "_display_free", lambda n: n == number or real_free(n)
            )
            record = _launch()
            assert record["display"] != f":{number}"
            assert sessions._lock_owner(number) == other.pid
        finally:
            other.terminate()
            other.wait()


NESTED_ESCAPEES = """
import subprocess, sys, time
# B leaves the group (setsid), ignores SIGTERM, and has a child C of its own.
b = subprocess.Popen([sys.executable, '-c',
    'import os, signal, subprocess, sys, time; '
    'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
    'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"]); '
    'print("nested", os.getpid(), c.pid, flush=True); time.sleep(600)'],
    start_new_session=True)
time.sleep(float(sys.argv[1]))
"""


def _nested_pids(session_id: str) -> tuple[int, int]:
    _wait_log(session_id, "nested ")
    words = sessions.read_log(session_id).split("nested ")[1].split()
    return int(words[0]), int(words[1])


def _all_gone(pids, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(sessions._start_time(pid) is None for pid in pids):
            return True
        time.sleep(0.1)
    return False


class TestSupervisor:
    def test_nested_escapees_end_when_the_app_exits(self):
        record = sessions.launch(
            [sys.executable, "-c", NESTED_ESCAPEES, "0.5"],
            name="nested",
            gpu="off",
            wait=0,
        )
        b, c = _nested_pids("nested")
        assert _all_gone([b, c]), "an escaped descendant survived the app's exit"
        deadline = time.monotonic() + 15
        while sessions.get("nested")["status"] != "exited":
            assert time.monotonic() < deadline
            time.sleep(0.1)
        stored = sessions._read_record("nested")
        assert stored is not None
        assert _all_gone([stored["app_pid"]])
        assert record["id"] == "nested"

    def test_nested_escapees_end_on_stop(self):
        sessions.launch(
            [sys.executable, "-c", NESTED_ESCAPEES, "600"],
            name="nested",
            gpu="off",
            wait=0,
        )
        b, c = _nested_pids("nested")
        record = sessions._read_record("nested")
        assert record is not None
        sessions.stop("nested")
        assert _all_gone([b, c, record["app_pid"], record["xvfb_pid"]])

    def test_a_pid_that_stopped_being_ours_is_not_signalled(self, monkeypatch):
        """Between the scan and the signal a PID can be reused: the pidfd is
        re-checked against /proc and an unrelated process is left alone."""
        from app import supervise

        out = subprocess.run(
            ["sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
            capture_output=True,
            text=True,
            check=True,
            start_new_session=True,
        )
        stranger = int(out.stdout.strip())  # reparented away: not ours
        try:
            monkeypatch.setattr(supervise, "_targets", lambda: [stranger])
            assert supervise.signal_ours(signal.SIGKILL) == 0
            time.sleep(0.2)
            assert sessions._start_time(stranger) is not None
        finally:
            os.kill(stranger, signal.SIGKILL)


def _system_gst() -> bool:
    probe = "import gi; gi.require_version('GstWebRTC', '1.0'); from gi.repository import GstWebRTC"
    return (
        subprocess.run(
            ["/usr/bin/python3", "-c", probe], capture_output=True
        ).returncode
        == 0
    )


@pytest.mark.skipif(
    not _system_gst(), reason="needs GStreamer WebRTC in the system Python"
)
def test_the_streamer_attaches_under_the_state_lock():
    record = sessions._read_record(_launch()["id"])
    assert record is not None
    streamer = Path(sessions.__file__).with_name("streamer.py")
    lock_path = sessions.apps_dir() / ".lock"
    with sessions._launch_lock():
        proc = subprocess.Popen(
            [
                "/usr/bin/python3",
                str(streamer),
                "--display",
                record["display"],
                "--codecs",
                "VP8",
                "--xvfb-pid",
                str(record["xvfb_pid"]),
                "--xvfb-start",
                str(record["xvfb_start"]),
                "--state-lock",
                str(lock_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            time.sleep(3)
            assert proc.poll() is None
            assert os.get_blocking(proc.stdout.fileno())
            import select

            ready, _, _ = select.select([proc.stdout], [], [], 0)
            assert not ready, "the streamer attached while the lock was held"
        except BaseException:
            proc.kill()
            raise
    try:
        import select

        ready, _, _ = select.select([proc.stdout], [], [], 15)
        assert ready, "the streamer never became ready after the lock was freed"
        line = proc.stdout.readline()
        assert '"ready"' in line, line
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


class TestAgentInputLocking:
    def test_long_typing_leaves_the_lock_free_and_ends_with_a_stop(self, tmp_path):
        _launch()
        log = tmp_path / "probe.log"
        outcome: list[BaseException] = []

        def typist():
            try:
                sessions.type_text("probe", "a" * 600)  # ~7 s of typing
            except (AppError, KeyError) as exc:
                outcome.append(exc)

        def typed() -> int:
            return log.read_text().count("keydown a") if log.exists() else 0

        worker = threading.Thread(target=typist)
        worker.start()
        try:
            # Typing really started, and is still going.
            deadline = time.monotonic() + 10
            while typed() < 10:
                assert time.monotonic() < deadline, f"typing never started: {outcome}"
                time.sleep(0.05)
            assert worker.is_alive() and not outcome

            asked = time.monotonic()
            with sessions._launch_lock():
                waited = time.monotonic() - asked
            assert waited < 1.5, f"the lock was held {waited:.1f} s by agent typing"
            assert worker.is_alive(), "typing ended on its own before the stop"

            asked = time.monotonic()
            sessions.stop("probe")
            assert time.monotonic() - asked < 8
        finally:
            worker.join(30)
        assert not worker.is_alive()
        # The stop ended it: the next step found the app gone.
        assert len(outcome) == 1 and isinstance(outcome[0], (KeyError, AppError))
        assert 10 <= typed() < 600
