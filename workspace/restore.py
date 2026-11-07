"""Rebuild a snapshot's missing sessions and resume their agents.

Ported from the ``reboot-restore`` skill's restore, which is proven. For each
snapshot session missing from tmux: create it detached, sized to its layout,
windows at their original index, panes split and the layout applied, each pane
started in its directory, window names and ``@agent_*`` options restamped, the
active window and pane selected. Each Claude / Codex agent is then typed into
its pane's fresh interactive shell (``send-keys``), so the user's shell config,
aliases and PATH apply and quitting the agent leaves a shell in the right
place. Nothing else is ever typed: other panes come back as a plain shell.

Two guards, in the engine so that every caller (the banner, ``merlin
workspace restore``) gets them:

- one restore at a time, across processes: ``restore.lock`` under the
  workspace directory, an advisory ``flock`` taken without waiting; a second
  restore fails at once with ``RestoreBusy``;
- no conversation launched twice, whatever the provider: a conversation is
  live when a Claude session file names it, or when a live window's
  ``@agent_provider`` / ``@agent_conv`` (stamped by the agents' SessionStart
  hook, Codex included) names it. Its pane comes back as a plain shell.

A session whose name is live is skipped, except a *fillable* one: the single
plain shell the web terminal auto-creates (``merlin-dev``) when it is opened
before Restore. The snapshot's windows are created inside it, then its original
window is killed (see ``docs/dev/workspace-restore.md``).

A session that fails halfway is rolled back, so that it is missing again and
the offer, which stays on a failure, can restore it on a retry: a session this
restore created is killed, a placeholder being filled loses the windows this
restore added and gets its original window back where it was. Without that,
the half-built session would count as present and the offer for it would be
dropped. A tmux id is unique within one server only: a replacement server on
the same socket (the web terminal recreating one the instant the old died)
hands out the same ids again, so every rollback command is bound to the
server that created the object, through ``if-shell -F`` on the server's own
pid and start time, checked and run in one round trip (``_owned``). On any
other server it does nothing.
"""

from __future__ import annotations

from collections.abc import Collection
import contextlib
import fcntl
import logging
import os
import re
import subprocess
import time

import paths
from terminal.tmux import (
    JOBS_SESSION_NAME,
    DEFAULT_SESSION_NAME,
    server_spawn_prefix,
    terminal_process_env,
    tmux_conf_args,
)

from . import snapshot as snap

logger = logging.getLogger("merlin.workspace")

# Pause after typing an agent's command, so several agents starting at once do
# not all hit the disk in the same instant (the skill's proven pacing).
_LAUNCH_PAUSE = 0.3


class RestoreError(RuntimeError):
    pass


class RestoreBusy(RestoreError):
    """Another restore holds ``restore.lock``, in this process or another."""


def lock_path():
    return paths.workspace_dir() / "restore.lock"


@contextlib.contextmanager
def restore_lock():
    """Hold the one restore slot for the duration of a restore. Never waits:
    a restore already running, from the banner or the CLI, in this process
    or another, makes this one fail at once with ``RestoreBusy``."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RestoreBusy("a restore is already running") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _env() -> dict[str, str]:
    return terminal_process_env(os.environ, term="xterm-256color")


def _tmux_quote(arg: str) -> str:
    """Quote one argument for tmux's own command parser (single quotes are
    literal there; a quote inside is closed, escaped and reopened)."""
    return "'" + arg.replace("'", "'\\''") + "'"


def _owned(server: tuple[int, int] | None, *command: str) -> None:
    """Run a tmux command on the server ``server`` (pid, start time) only.

    The identity test and the command run inside one server round trip
    (``if-shell -F``): a replacement server listening on the same socket
    evaluates the test false and runs nothing, so a rollback can never kill
    or move an object it did not create. With no identity, nothing runs.
    """
    if server is None:
        logger.warning(
            "Workspace restore: rollback of %s skipped, server identity unknown",
            command[0],
        )
        return
    pid, start = server
    test = f"#{{&&:#{{==:#{{pid}},{pid}}},#{{==:#{{start_time}},{start}}}}}"
    _tmux(
        "if-shell", "-F", test, " ".join(_tmux_quote(a) for a in command), check=False
    )


def _identity(pid: str, start: str) -> tuple[int, int] | None:
    try:
        return int(pid), int(start)
    except ValueError:
        return None


def _tmux(*args: str, check: bool = True, prefix: list[str] | None = None) -> str:
    argv = [*(prefix or []), "tmux", *tmux_conf_args(), *args]
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, env=_env(), timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RestoreError(f"tmux {' '.join(args)}: {exc}") from exc
    if check and r.returncode != 0:
        raise RestoreError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


# --- what is missing --------------------------------------------------------


def live_sessions() -> dict[str, bool] | None:
    """Live session name -> fillable, or None when there is no tmux server.

    Fillable: the session the web terminal creates on its own
    (``DEFAULT_SESSION_NAME``) while it is still exactly one window holding one
    pane that runs a bare shell. Any other live name is never fillable.
    """
    rows = snap.read_tmux()
    if rows is None:
        return None
    return live_sessions_of(rows)


def live_sessions_of(
    rows: tuple[list[dict], list[dict], list[dict]],
) -> dict[str, bool]:
    """``live_sessions`` from rows already read (the sweep reads once)."""
    sessions, windows, panes = rows
    windows_of: dict[str, list[dict]] = {}
    for w in windows:
        windows_of.setdefault(w["session_name"], []).append(w)
    panes_of: dict[str, list[dict]] = {}
    for p in panes:
        panes_of.setdefault(p["window_id"], []).append(p)
    out: dict[str, bool] = {}
    for s in sessions:
        name = s["session_name"]
        wins = windows_of.get(name, [])
        fillable = False
        if name == DEFAULT_SESSION_NAME and len(wins) == 1:
            ps = panes_of.get(wins[0]["window_id"], [])
            fillable = len(ps) == 1 and ps[0]["pane_current_command"] in snap.SHELLS
        out[name] = fillable
    return out


def _is_bare(sess: dict) -> bool:
    """A snapshot session that is itself one window, one pane, no agent."""
    windows = sess.get("windows") or []
    if len(windows) != 1:
        return False
    panes = windows[0].get("panes") or []
    return len(panes) <= 1 and not any(p.get("agent") for p in panes)


def live_job_runs() -> set[str]:
    """The runs whose window is alive in tmux right now."""
    rows = snap.read_tmux()
    if rows is None:
        return set()
    return job_runs_of(rows)


def job_runs_of(rows: tuple[list[dict], list[dict], list[dict]]) -> set[str]:
    """``live_job_runs`` from rows already read."""
    return {w["@job_run"] for w in rows[1] if w.get("@job_run")}


def _without_live_kept(sessions: list[dict], job_runs: set[str]) -> list[dict]:
    """A kept job window is saved under ``merlin-jobs-kept``, but while tmux
    lives it is still in ``merlin-jobs``: only one whose run has no window left
    is lost. Without this, a mere Merlin restart would offer to bring back, and
    duplicate, windows that never went away."""
    out = []
    for sess in sessions:
        if sess["name"] == snap.KEPT_JOBS_SESSION:
            windows = [
                w for w in sess.get("windows", []) if w.get("job_run") not in job_runs
            ]
            if not windows:
                continue
            sess = {**sess, "windows": windows}
        out.append(sess)
    return out


def missing_sessions(
    snapshot: dict, live: dict[str, bool] | None, job_runs: Collection[str] = ()
) -> list[dict]:
    """Snapshot sessions a restore would bring back: absent from tmux, or
    present only as the terminal's fresh placeholder while the snapshot holds
    more than one bare shell. The placeholder check is by shape, not by name,
    because the web terminal can create ``merlin-dev`` before the freeze runs.
    ``job_runs`` are the runs still alive (``live_job_runs``).
    """
    live = live or {}
    out = []
    for s in _without_live_kept(snapshot["sessions"], set(job_runs)):
        if s["name"] == JOBS_SESSION_NAME:
            continue  # a job run is not part of the workspace (older snapshots)
        if s["name"] not in live:
            out.append(s)
        elif live[s["name"]] and not _is_bare(s):
            out.append(s)
    return out


# --- restore ----------------------------------------------------------------


def _start_dir(pane: dict) -> str:
    agent = pane.get("agent") or {}
    for d in (agent.get("cwd"), pane.get("cwd")):
        if d and os.path.isdir(d):
            return d
    return str(os.path.expanduser("~"))


def layout_size(layout: str) -> tuple[str, str]:
    m = re.match(r"^[0-9a-f]{4},(\d+)x(\d+),", layout or "")
    return (m.group(1), m.group(2)) if m else ("200", "50")


def live_conversations(
    rows: tuple[list[dict], list[dict], list[dict]] | None,
) -> set[tuple[str, str]]:
    """``(provider, conversation)`` of every conversation running right now:
    the live Claude session files, and the ``@agent_provider`` /
    ``@agent_conv`` of every live window (the hook stamps both agents, so
    this covers Codex). A restore never launches one of these again."""
    out: set[tuple[str, str]] = set()
    proc = snap.Proc()
    for record in snap.live_claude_sessions(proc).values():
        if record.get("sessionId"):
            out.add(("claude", record["sessionId"]))
    if rows is not None:
        for w in rows[1]:
            provider, conv = w.get("@agent_provider", ""), w.get("@agent_conv", "")
            if provider and conv:
                out.add((provider, conv))
    return out


def _fill_window(win: dict, wid: str, live: set[tuple[str, str]]) -> int:
    """Split the panes of a freshly created window, restamp its options and
    type the agents. Returns the number of agents launched."""
    panes = sorted(win["panes"], key=lambda p: p["index"])
    pane_ids = [_tmux("display-message", "-p", "-t", wid, "#{pane_id}")]
    for pane in panes[1:]:
        pane_ids.append(
            _tmux(
                "split-window",
                "-d",
                "-t",
                pane_ids[-1],
                "-c",
                _start_dir(pane),
                "-P",
                "-F",
                "#{pane_id}",
            )
        )
        # Rebalance after each split: splitting the newest pane again and
        # again halves it until tmux has no room left, even when the saved
        # layout fits easily. The saved layout is applied once all exist.
        _tmux("select-layout", "-t", wid, "tiled", check=False)
    if len(panes) > 1:
        try:
            _tmux("select-layout", "-t", wid, win.get("layout", ""))
        except RestoreError:
            logger.warning(
                "Workspace restore: layout of %s not applied, panes left tiled",
                wid,
                exc_info=True,
            )
    if win.get("automatic_rename"):
        _tmux("set-option", "-w", "-t", wid, "automatic-rename", "on", check=False)
    for key, value in (win.get("options") or {}).items():
        if key in snap.WINDOW_OPTIONS:
            _tmux("set-option", "-w", "-t", wid, key, str(value), check=False)
    launched = 0
    for pane_id, pane in zip(pane_ids, panes):
        if pane.get("active"):
            _tmux("select-pane", "-t", pane_id, check=False)
        agent = pane.get("agent")
        if not agent or not agent.get("conv"):
            continue
        if (agent["provider"], agent["conv"]) in live:
            continue  # already running elsewhere: never launched twice
        _tmux(
            "send-keys",
            "-t",
            pane_id,
            "-l",
            snap.shell_quote(snap.resume_command(agent)),
        )
        _tmux("send-keys", "-t", pane_id, "Enter")
        launched += 1
        time.sleep(_LAUNCH_PAUSE)
    return launched


def _restore_new(
    sess: dict, server_running: bool, live_claude: set[tuple[str, str]]
) -> int:
    name = sess["name"]
    windows = sorted(sess["windows"], key=lambda w: w["index"])
    launched = 0
    session_id = ""
    server: tuple[int, int] | None = None
    try:
        for n, win in enumerate(windows):
            target = f"={name}:{win['index']}"
            panes = sorted(win["panes"], key=lambda p: p["index"])
            first_dir = _start_dir(panes[0]) if panes else os.path.expanduser("~")
            if n == 0:
                cols, rows = layout_size(win.get("layout", ""))
                created = _tmux(
                    "new-session",
                    "-d",
                    "-s",
                    name,
                    "-n",
                    win["name"],
                    "-c",
                    first_dir,
                    "-x",
                    cols,
                    "-y",
                    rows,
                    "-P",
                    "-F",
                    "#{session_id}\t#{window_id}\t#{pid}\t#{start_time}",
                    prefix=server_spawn_prefix(server_running),
                )
                session_id, wid, pid, start = created.split("\t", 3)
                server = _identity(pid, start)
                server_running = True
                index = _tmux("display-message", "-p", "-t", wid, "#{window_index}")
                if index != str(win["index"]):
                    _tmux("move-window", "-s", wid, "-t", target)
            else:
                wid = _tmux(
                    "new-window",
                    "-d",
                    "-t",
                    target,
                    "-n",
                    win["name"],
                    "-c",
                    first_dir,
                    "-P",
                    "-F",
                    "#{window_id}",
                )
            if panes:
                launched += _fill_window(win, wid, live_claude)
    except RestoreError:
        if session_id:
            # Only the session this restore just created, on the server that
            # created it: a retry must find it missing, not half built and
            # counted as present.
            _owned(server, "kill-session", "-t", session_id)
            logger.info("Workspace restore: rolled back the half-built %s", name)
        raise
    return launched


def _restore_into(sess: dict, live_claude: set[tuple[str, str]]) -> int:
    """Fill the auto-created single-shell session with the snapshot's windows,
    then drop its original window."""
    name = sess["name"]
    windows = sorted(sess["windows"], key=lambda w: w["index"])
    original, original_index, pid, start = _tmux(
        "display-message",
        "-p",
        "-t",
        f"={name}:",
        "#{window_id}\t#{window_index}\t#{pid}\t#{start_time}",
    ).split("\t", 3)
    server = _identity(pid, start)
    # Park the original window above every snapshot index so none collides.
    park = max([w["index"] for w in windows] + [0]) + 1
    _tmux("move-window", "-s", original, "-t", f"={name}:{park}", check=False)
    launched = 0
    added: list[str] = []
    try:
        for win in windows:
            panes = sorted(win["panes"], key=lambda p: p["index"])
            first_dir = _start_dir(panes[0]) if panes else os.path.expanduser("~")
            wid = _tmux(
                "new-window",
                "-d",
                "-t",
                f"={name}:{win['index']}",
                "-n",
                win["name"],
                "-c",
                first_dir,
                "-P",
                "-F",
                "#{window_id}",
            )
            added.append(wid)
            if panes:
                launched += _fill_window(win, wid, live_claude)
    except RestoreError:
        # Back to the placeholder it was: the windows this restore added go,
        # the original window returns to its index, so the session is
        # fillable again and the offer still lists it.
        for wid in added:
            _owned(server, "kill-window", "-t", wid)
        _owned(server, "move-window", "-s", original, "-t", f"={name}:{original_index}")
        logger.info("Workspace restore: rolled back the half-filled %s", name)
        raise
    _select_active(sess)
    # The user may have started something in the original shell while the
    # restore ran: drop the window only if it is still one bare shell.
    commands = _tmux(
        "list-panes", "-t", original, "-F", "#{pane_current_command}", check=False
    ).splitlines()
    if len(commands) == 1 and commands[0] in snap.SHELLS:
        _tmux("kill-window", "-t", original, check=False)
    else:
        logger.info("Workspace restore: kept %s's original window, now in use", name)
    return launched


def _select_active(sess: dict) -> None:
    active = sess.get("active_window")
    if active is not None:
        _tmux("select-window", "-t", f"={sess['name']}:{active}", check=False)


def restore(snapshot: dict) -> dict:
    """Bring back every missing session of ``snapshot``.

    Returns ``{"restored": [names], "skipped": [names], "agents": n,
    "failed": [names]}``. One session failing does not stop the others.
    Raises ``RestoreBusy`` at once when another restore is running.
    """
    with restore_lock():
        return _restore(snapshot)


def _restore(snapshot: dict) -> dict:
    rows = snap.read_tmux()
    server_running = rows is not None
    live = live_sessions_of(rows) if rows is not None else {}
    job_runs = job_runs_of(rows) if rows is not None else set()
    live_claude = live_conversations(rows)
    result: dict = {"restored": [], "skipped": [], "failed": [], "agents": 0}
    for sess in _without_live_kept(snapshot["sessions"], job_runs):
        name = sess["name"]
        if not sess.get("windows") or name == JOBS_SESSION_NAME:
            continue
        try:
            if name not in live:
                result["agents"] += _restore_new(sess, server_running, live_claude)
                server_running = True
                _select_active(sess)
            elif live[name]:
                result["agents"] += _restore_into(sess, live_claude)
            else:
                result["skipped"].append(name)
                continue
        except RestoreError:
            logger.warning("Workspace restore: session %s failed", name, exc_info=True)
            result["failed"].append(name)
            # The rollback may have killed the only session, and the server
            # with it: the next session must spawn the server properly.
            server_running = snap.live_session_names() is not None
            continue
        result["restored"].append(name)
    logger.info(
        "Workspace restore: %d sessions restored, %d skipped, %d failed, %d agents",
        len(result["restored"]),
        len(result["skipped"]),
        len(result["failed"]),
        result["agents"],
    )
    return result
