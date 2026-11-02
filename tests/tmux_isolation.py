"""Keeping every test away from the user's real tmux server.

Two rules, learned the hard way (a teardown once killed a working server of
24 windows and 22 agents):

1. **A server is killed by its socket, never by an environment variable.**
   ``tmux kill-server`` with no ``-S`` resolves its target from ``$TMUX``,
   then from ``$TMUX_TMPDIR``, and when that directory does not exist any
   more it silently falls back to ``/tmp/tmux-<uid>/default``: the real
   server. ``kill_server`` below always passes ``-S`` with the socket of the
   given directory, and does nothing when that socket is gone.

2. **A bare ``tmux`` whose ``TMUX_TMPDIR`` has vanished must fail, not fall
   back.** ``install_guard`` puts a ``tmux`` shim first on ``PATH`` for the
   whole pytest session. The shim forwards a call that names its socket
   (``-S``), or runs inside a test server (``$TMUX`` with a live socket that
   is not the real default one), or has a ``TMUX_TMPDIR`` that exists; every
   other call is refused with exit 1. Merlin's own code calls ``tmux`` bare
   and relies on ``TMUX_TMPDIR``, so this is what keeps a job window, a board
   sweep or a workspace restore under test off the real server even after
   a socket directory was removed.

Two pytest runs may overlap (an e2e run and a unit run side by side): a run
never deletes a directory that another run may still be using. Stale
directories of interrupted runs are removed only once they are old.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

STALE_AFTER_SECONDS = 6 * 3600

_SHIM = """#!/bin/sh
# Merlin test guard (tests/tmux_isolation.py): a tmux call that could reach
# the user's real server is refused instead of silently falling back to it.
real={real}
for a in "$@"; do
  case "$a" in
    -S|-S?*) exec "$real" "$@" ;;
  esac
done
if [ -n "$TMUX" ]; then
  sock="${{TMUX%%,*}}"
  case "$sock" in
    {real_dir}/*) ;;
    *) if [ -S "$sock" ]; then exec "$real" "$@"; fi ;;
  esac
fi
if [ -n "$TMUX_TMPDIR" ] && [ -d "$TMUX_TMPDIR" ]; then
  exec "$real" "$@"
fi
echo "merlin tests: tmux refused ($*): TMUX_TMPDIR is unset or gone" \\
  "(${{TMUX_TMPDIR:-unset}}), the call would reach the real server" >&2
exit 1
"""


def real_socket_dir() -> Path:
    """Where tmux keeps its sockets when nothing redirects it."""
    return Path(os.environ.get("TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"


def socket_path(tmux_dir: str | Path) -> Path:
    """The default socket of a server whose ``TMUX_TMPDIR`` is ``tmux_dir``."""
    return Path(tmux_dir) / f"tmux-{os.getuid()}" / "default"


def kill_server(tmux_dir: str | Path, *, run=subprocess.run) -> bool:
    """Kill the server of ``tmux_dir``'s default socket, by that socket.
    Returns False without running anything when the socket is gone: a
    missing socket is never a reason to fall back to the real server."""
    sock = socket_path(tmux_dir)
    if not sock.exists():
        return False
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    run(
        ["tmux", "-S", str(sock), "kill-server"],
        env=env,
        capture_output=True,
        check=False,
    )
    return True


def write_shim(bin_dir: Path, real_tmux: str) -> Path:
    """Write the guarding ``tmux`` shim into ``bin_dir`` and return it."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "tmux"
    shim.write_text(_SHIM.format(real=real_tmux, real_dir=real_socket_dir()))
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def install_guard(session_dir: Path) -> Path | None:
    """Put the shim first on ``PATH`` for this process and its children.
    Returns the shim, or None when tmux is not installed."""
    real = shutil.which("tmux")
    if real is None:
        return None
    shim = write_shim(session_dir / "bin", real)
    os.environ["PATH"] = f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    return shim


def new_session_dir() -> Path:
    """A private socket directory for one pytest session. Short, since unix
    socket paths cap around 108 characters."""
    return Path(tempfile.mkdtemp(prefix="merlin-tmux-"))


def remove_stale(prefix: str, keep: Path, *, now: float | None = None) -> list[Path]:
    """Remove the ``<tmpdir>/<prefix>*`` directories older than
    ``STALE_AFTER_SECONDS``, except ``keep``: leftovers of interrupted runs,
    never the directory of a run that may still be going. A tmux directory's
    server is killed first, by its socket."""
    now = time.time() if now is None else now
    removed: list[Path] = []
    for stale in Path(tempfile.gettempdir()).glob(f"{prefix}*"):
        if stale == keep or not stale.is_dir():
            continue
        try:
            age = now - stale.stat().st_mtime
        except OSError:
            continue
        if age < STALE_AFTER_SECONDS:
            continue
        kill_server(stale)
        shutil.rmtree(stale, ignore_errors=True)
        removed.append(stale)
    return removed
