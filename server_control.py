"""Stop / relaunch the running Merlin server.

This is the detached helper behind `merlin restart`, `merlin stop`, and the
`/api/restart` + `/api/update` endpoints. It replaces the old `restart.sh`.

Two properties are deliberate and load-bearing:

  * **It only ever runs as a detached process.** You cannot restart a process
    from inside itself — the caller (`/api/restart`, or a shell) spawns
    `merlin restart` in a new session so it outlives the server it kills.

  * **It stays import-light** — only `paths` and the stdlib. During an update the
    `~/.merlin/current` symlink is flipped *before* the restart runs, so this
    code may execute as a different version than the one that imported it. Fewer
    imports means fewer ways for a half-swapped tree to break the restarter. The
    relaunch itself is delegated to a fresh `uv run`, which resolves the new
    version's venv cleanly rather than reusing this process's interpreter.

Stopping targets the exact PID from server-state.json (paths.server_state_path),
validated with POSIX `ps` so a recycled PID is never signalled. A user-scoped
pattern-kill remains as a gated fallback for the first restart after updating
from a version that predates the PID file; it is removable once every supported
release writes one.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

import paths

# Fallback command-line patterns, used ONLY when the PID path stops nothing.
# REMOVE once every supported release writes a PID file.
_FALLBACK_PATTERNS = (
    "uv run cli.py",
    "uv run main.py",
    "python main.py",
)


def _read_legacy_pid() -> int | None:
    """PID from the legacy server-pid file (a server started by v0.30.x, before
    the unified server-state.json). Port is unknown there."""
    try:
        raw = paths.server_pid_path().read_text()
    except OSError:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


def live_state() -> tuple[int | None, int | None]:
    """(pid, port) of the running server, or (None, None).

    Prefer the unified server-state.json; fall back to the legacy server-pid
    file (port unknown there). Only ever returns a PID that is a live Merlin
    process — a stale file naming a dead or foreign PID yields (None, None).
    """
    state = paths.read_server_state()
    if state is not None:
        pid = state.get("pid")
        port = state.get("port")
        if isinstance(pid, int) and pid_is_merlin(pid):
            return pid, (port if isinstance(port, int) else None)

    legacy = _read_legacy_pid()
    if legacy is not None and pid_is_merlin(legacy):
        return legacy, None
    return None, None


def _remove_state_files() -> None:
    """Drop the state file and the legacy pid file, if present."""
    for path in (paths.server_state_path(), paths.server_pid_path()):
        try:
            path.unlink()
        except OSError:
            pass


def _ps_field(pid: int, fmt: str) -> str | None:
    """One `ps -o <fmt>=` field for pid, or None. POSIX; Linux and macOS.

    `-ww` disables the default column truncation so a long command line (a deep
    install path) still shows the cli.py/main.py marker we match on.
    """
    try:
        out = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", fmt],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def pid_is_merlin(pid: int) -> bool:
    """True only if pid is a live process we own that looks like Merlin.

    PIDs get recycled, so a stale file can name a stranger — we never signal a
    PID we cannot positively identify. Ownership uses the numeric uid (not the
    username, which `ps` can truncate). Identity checks that the command line
    contains cli.py or main.py.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists but not ours; the uid check below rejects it
    except OSError:
        return False

    uid = _ps_field(pid, "uid=")
    if uid is None or not uid.isdigit() or int(uid) != os.getuid():
        return False

    args = _ps_field(pid, "args=")
    return bool(args) and ("cli.py" in args or "main.py" in args)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # recycled to another owner: our target is gone
    except OSError:
        return False
    return True


def _parent_pid(pid: int) -> int | None:
    ppid = _ps_field(pid, "ppid=")
    return int(ppid) if ppid and ppid.isdigit() else None


def _own_lineage() -> set[int]:
    """This process and its ancestors. The restarter runs as `uv run cli.py
    restart`, whose wrapper matches a fallback pattern, so signalling any of
    these would kill the restart itself mid-relaunch."""
    lineage = {os.getpid()}
    pid = os.getppid()
    for _ in range(64):  # bounded walk toward PID 1
        if pid <= 1 or pid in lineage:
            break
        lineage.add(pid)
        parent = _parent_pid(pid)
        if parent is None:
            break
        pid = parent
    return lineage


def _fallback_pattern_kill() -> None:
    """Gated legacy stop: user-scoped SIGTERM to processes whose command line
    matches a Merlin pattern — EXCEPT this restarter's own lineage and process
    group. Runs only when the PID path stopped nothing.

    The exclusion is essential: `merlin restart` runs as `uv run cli.py restart`,
    whose wrapper matches "uv run cli.py"; a blind `pkill` would signal that
    wrapper and `uv` forwards the signal to the child doing the relaunch, killing
    it. The old daemon we DO want to stop lives in a different process group, so
    it is still signalled.
    """
    uid = str(os.getuid())
    protected = _own_lineage()
    try:
        my_pgid = os.getpgrp()
    except OSError:
        my_pgid = -1

    victims: set[int] = set()
    for pattern in _FALLBACK_PATTERNS:
        try:
            out = subprocess.run(
                ["pgrep", "-u", uid, "-f", pattern], capture_output=True, text=True
            )
        except OSError:
            continue
        for token in out.stdout.split():
            if token.isdigit():
                victims.add(int(token))

    for pid in victims:
        if pid in protected:
            continue
        try:
            if os.getpgid(pid) == my_pgid:  # our own restart process group
                continue
        except OSError:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def stop_server(timeout: float = 5.0) -> tuple[bool, int | None]:
    """Stop the running server. Returns (stopped, port) — whether a process was
    signalled, and the port it was on (for restart to reuse), or None.

    SIGTERM, wait up to `timeout`, then SIGKILL only if still alive. The state
    files are removed afterwards whether we signalled a process or found them
    stale. If nothing valid was found, the gated fallback runs.
    """
    pid, port = live_state()
    stopped = False

    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        stopped = True
        print(f"Stopped Merlin (PID {pid})")

    _remove_state_files()

    if not stopped:
        _fallback_pattern_kill()

    return stopped, port


def _relaunch_env() -> dict[str, str]:
    """Environment for the relaunched server, independent of the launch shell.

    Merlin is a daemon: it must not inherit a tmux client's TMUX/TMUX_PANE or a
    dumb TERM from whichever pane restarted it, nor that pane's old copy of a
    setting config.env holds: a restart applies the file. A setting the file
    does not hold still passes through, and so does the server's own
    credential that only its environment had. (Kept local rather than importing
    terminal.tmux, to keep this module import-light — see the module docstring.)
    """
    env = dict(os.environ)
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    for key in paths.own_settings_in_config():
        env.pop(key, None)
    # A terminal never holds the server's credentials: they come back from
    # where the server left them (a value the shell does hold still wins).
    for key, value in paths.read_server_secrets().items():
        env.setdefault(key, value)
    env["TERM"] = "xterm-256color"
    return env


def _relaunch(port: int | None = None) -> subprocess.Popen:
    """Start a fresh, detached server via `uv run`, on the same port it was on.

    `uv run` re-resolves the venv for the app directory (the new version after an
    update's symlink flip), so the successor never inherits this process's pinned
    interpreter or sys.path. `port` is the port the stopped server was bound to;
    passing it as an explicit `--port` preserves a custom port across restart.
    When unknown (None), the child re-resolves normally (MERLIN_DASHBOARD_PORT,
    then 3123).
    """
    app_dir = paths.app_dir()
    cmd = ["uv", "run", "cli.py", "start"]
    if port is not None:
        cmd += ["--port", str(port)]
    out = open(_relaunch_log(), "ab")  # noqa: SIM115 — handed to the child
    return subprocess.Popen(
        cmd,
        cwd=str(app_dir),
        env=_relaunch_env(),
        stdout=out,
        stderr=out,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def _relaunch_log():
    """Where a relaunched server's stdout and stderr go: the reason a start
    was refused (a missing tmux, a bad config) is written there."""
    return paths.app_dir() / "nohup.out"


# How long `merlin restart` waits for the relaunched server to bind.
START_TIMEOUT = 20.0


def _descends_from(pid: int, ancestor: int, depth: int = 4) -> bool:
    """Whether `pid` is `ancestor` or one of its descendants, a few levels
    deep. The relaunch is `uv run cli.py start`: uv is the process we hold,
    the server that publishes its state is the Python it starts under it."""
    for _ in range(depth):
        if pid == ancestor:
            return True
        parent = _parent_pid(pid)
        if parent is None or parent <= 1:
            return False
        pid = parent
    return pid == ancestor


def _wait_for_start(proc: subprocess.Popen, timeout: float = START_TIMEOUT) -> str:
    """Watch the relaunched server until it publishes its state (it bound its
    port), exits, or `timeout` passes. Returns "started", "exited" or
    "pending". The state is the relaunch's only when the live Merlin that
    wrote it runs under `proc` (uv, then its Python child)."""
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            return "exited"
        pid, _ = live_state()
        if pid is not None and _descends_from(pid, proc.pid):
            return "started"
        if time.monotonic() >= deadline:
            return "pending"
        time.sleep(0.2)


def _log_tail(lines: int = 12) -> str:
    """The last `lines` lines of the relaunch log, or "" when it cannot be read."""
    try:
        with open(_relaunch_log(), "rb") as f:
            data = f.read()
    except OSError:
        return ""
    return b"\n".join(data.splitlines()[-lines:]).decode("utf-8", "replace")


def restart() -> int:
    """Stop the server, then bring it back — unless a supervisor owns that.

    Under MERLIN_SUPERVISED the service manager starts the replacement, so we
    stop and exit; launching our own would race it for the port. Returns a
    process exit code.
    """
    _, port = stop_server()
    time.sleep(1)

    if paths.is_supervised():
        print("Supervised mode: stopped; the service manager will restart Merlin.")
        return 0

    proc = _relaunch(port)
    outcome = _wait_for_start(proc, START_TIMEOUT)
    if outcome == "started":
        print("Merlin restarted.")
        return 0
    log = _relaunch_log()
    if outcome == "pending":
        print(
            f"Merlin is still starting (PID {proc.pid}). If it does not come up, see {log}"
        )
        return 0
    print(
        f"Merlin failed to start (exit status {proc.returncode}). The reason is in {log}:"
    )
    tail = _log_tail()
    if tail:
        print("\n".join("  " + line for line in tail.splitlines()))
    return 1


def stop() -> int:
    """Stop the server without relaunching. Returns a process exit code."""
    stopped, _ = stop_server()
    print("Merlin stopped." if stopped else "No running Merlin found (state cleared).")
    return 0
