"""Rebuild a snapshot's missing sessions and resume their agents.

Ported from the ``reboot-restore`` skill's restore, which is proven. For each
snapshot session missing from tmux: create it detached, sized to its layout,
windows at their original index, panes split and the layout applied, each pane
started in its directory, window names and ``@agent_*`` options restamped, the
active window and pane selected. Each Claude / Codex agent is then typed into
its pane's fresh interactive shell (``send-keys``), so the user's shell config,
aliases and PATH apply and quitting the agent leaves a shell in the right
place. Nothing else is ever typed: other panes come back as a plain shell.

A session whose name is live is skipped, except a *fillable* one: the single
plain shell the web terminal auto-creates (``merlin-dev``) when it is opened
before Restore. The snapshot's windows are created inside it, then its original
window is killed (see ``docs/dev/workspace-restore.md``).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time

from terminal.tmux import (
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


def _env() -> dict[str, str]:
    return terminal_process_env(os.environ, term="xterm-256color")


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


def missing_sessions(snapshot: dict, live: dict[str, bool] | None) -> list[dict]:
    """Snapshot sessions a restore would bring back: absent from tmux, or
    present only as the terminal's fresh placeholder while the snapshot holds
    more than one bare shell. The placeholder check is by shape, not by name,
    because the web terminal can create ``merlin-dev`` before the freeze runs.
    """
    live = live or {}
    out = []
    for s in snapshot["sessions"]:
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


def _live_claude_convs() -> set[str]:
    proc = snap.Proc()
    return {
        r.get("sessionId")
        for r in snap.live_claude_sessions(proc).values()
        if r.get("sessionId")
    }


def _fill_window(win: dict, wid: str, live_claude: set[str]) -> int:
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
        if agent["provider"] == "claude" and agent["conv"] in live_claude:
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


def _restore_new(sess: dict, server_running: bool, live_claude: set[str]) -> int:
    name = sess["name"]
    windows = sorted(sess["windows"], key=lambda w: w["index"])
    launched = 0
    for n, win in enumerate(windows):
        target = f"={name}:{win['index']}"
        panes = sorted(win["panes"], key=lambda p: p["index"])
        first_dir = _start_dir(panes[0]) if panes else os.path.expanduser("~")
        if n == 0:
            cols, rows = layout_size(win.get("layout", ""))
            wid = _tmux(
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
                "#{window_id}",
                prefix=server_spawn_prefix(server_running),
            )
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
    return launched


def _restore_into(sess: dict, live_claude: set[str]) -> int:
    """Fill the auto-created single-shell session with the snapshot's windows,
    then drop its original window."""
    name = sess["name"]
    windows = sorted(sess["windows"], key=lambda w: w["index"])
    original = _tmux("display-message", "-p", "-t", f"={name}:", "#{window_id}")
    # Park the original window above every snapshot index so none collides.
    park = max([w["index"] for w in windows] + [0]) + 1
    _tmux("move-window", "-s", original, "-t", f"={name}:{park}", check=False)
    launched = 0
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
        if panes:
            launched += _fill_window(win, wid, live_claude)
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
    """
    live = live_sessions()
    server_running = live is not None
    live = live or {}
    live_claude = _live_claude_convs()
    result: dict = {"restored": [], "skipped": [], "failed": [], "agents": 0}
    for sess in snapshot["sessions"]:
        name = sess["name"]
        if not sess.get("windows"):
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
