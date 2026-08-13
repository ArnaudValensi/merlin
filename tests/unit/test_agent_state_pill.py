"""Tests for the agent-state tmux pill (terminal/tmux.conf).

Loads the ACTUAL shipped tmux.conf into an isolated tmux server (its own
socket, HOME pointed at an empty temp dir so `source-file -q ~/.tmux.conf`
finds nothing) and renders window-status-format / window-status-current-format
across the five states (idle / busy / ask / done / unset). This validates the real
format strings, including the gating fallback and the split-style comma fix,
rather than a copy.

Skips cleanly when tmux is not installed (the web terminal already degrades
gracefully in that case).
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TMUX_CONF = REPO_ROOT / "terminal" / "tmux.conf"

# Colours the pill uses (see terminal/tmux.conf).
GREY = "#6272a4"
AMBER = "#f9e2af"
GREEN = "#a6e3a1"
PURPLE = "#bd93f9"
SKY = "#89dceb"

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


class TmuxServer:
    """A throwaway tmux server on a private socket, loading the real conf."""

    def __init__(self, socket: Path, home: Path, conf: Path | None):
        self.socket = str(socket)
        self.home = str(home)
        self.conf = conf

    def _base(self) -> list[str]:
        cmd = ["tmux", "-S", self.socket]
        if self.conf is not None:
            cmd += ["-f", str(self.conf)]
        return cmd

    def _env(self) -> dict[str, str]:
        # Isolated HOME (so `source-file -q ~/.tmux.conf` finds nothing) but a
        # UTF-8 locale carried through, or tmux replaces the wide pill glyphs
        # (◐ ●) with '_'.
        env = {"HOME": self.home, "TERM": "xterm-256color"}
        for var in ("LANG", "LC_ALL", "LC_CTYPE"):
            val = os.environ.get(var)
            if val:
                env[var] = val
        env.setdefault("LC_ALL", "C.UTF-8")
        return env

    def run(self, *args: str) -> str:
        result = subprocess.run(
            [*self._base(), *args],
            capture_output=True,
            text=True,
            env=self._env(),
            check=False,
        )
        return result.stdout.rstrip("\n")

    def kill(self) -> None:
        """Best-effort teardown. Never raises: this runs after the assertions,
        so a fork failure or a wedged tmux under load would otherwise turn a
        passing test into a teardown ERROR that says nothing about the pill."""
        try:
            subprocess.run(
                [*self._base(), "kill-server"],
                capture_output=True,
                env=self._env(),
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass


@pytest.fixture
def server(tmp_path):
    """Isolated tmux server with the shipped conf loaded, one window per state.

    Every window id is asserted non-empty. A newly forked tmux server can lose
    the race with the first `display-message` on a loaded machine and answer
    with "", which would silently retarget every later `-t ''` at the CURRENT
    window: the pill then renders the classic fallback and the failure reads as
    a bogus "wrong colour" rather than "the fixture never set up".
    """
    home = tmp_path / "home"
    home.mkdir()
    srv = TmuxServer(tmp_path / "s.sock", home, TMUX_CONF)
    srv.kill()
    srv.run("new-session", "-d", "-s", "t", "-x", "120", "-y", "40")
    # renumber-windows is on in the conf; use window ids to be robust.
    wins = {}
    for _ in range(50):  # ~5s ceiling; normally answers on the first try
        wins["idle"] = srv.run("display-message", "-p", "#{window_id}")
        if wins["idle"]:
            break
        time.sleep(0.1)
    for name in ("busy", "ask", "done", "unset"):
        wins[name] = srv.run("new-window", "-P", "-F", "#{window_id}", "-t", "t")
    assert all(wins.values()), f"tmux did not return every window id: {wins}"
    srv.run("set-option", "-w", "-t", wins["idle"], "@agent_state", "idle")
    srv.run("set-option", "-w", "-t", wins["busy"], "@agent_state", "busy")
    srv.run("set-option", "-w", "-t", wins["ask"], "@agent_state", "ask")
    srv.run("set-option", "-w", "-t", wins["done"], "@agent_state", "done")
    # wins["unset"] intentionally has no @agent_state.
    yield srv, wins
    srv.kill()


def render(srv: TmuxServer, win: str, current: bool) -> str:
    """Render the (inactive|active) window-status format for a window.

    #{E:...} expands the option value and then expands the formats inside it,
    so the result is exactly the pill tmux would draw for that window.
    """
    opt = "window-status-current-format" if current else "window-status-format"
    return srv.run("display-message", "-p", "-t", win, f"#{{E:{opt}}}")


class TestInactivePill:
    """window-status-format: glyph + colour per state, classic fallback."""

    def test_idle_grey_open_circle(self, server):
        srv, wins = server
        out = render(srv, wins["idle"], current=False)
        assert "○" in out
        assert GREY in out

    def test_busy_amber_half_moon(self, server):
        srv, wins = server
        out = render(srv, wins["busy"], current=False)
        assert "◐" in out
        assert AMBER in out

    def test_done_green_full_circle(self, server):
        srv, wins = server
        out = render(srv, wins["done"], current=False)
        assert "●" in out
        assert GREEN in out

    def test_ask_sky_question_mark(self, server):
        srv, wins = server
        out = render(srv, wins["ask"], current=False)
        assert "?" in out
        assert SKY in out
        # It must not fall through to any other state's glyph.
        for glyph in ("○", "◐", "●"):
            assert glyph not in out

    def test_unset_falls_back_to_classic(self, server):
        srv, wins = server
        out = render(srv, wins["unset"], current=False)
        # Classic inactive is a bare " ○ " with no inline colour override.
        assert "○" in out
        assert "#[" not in out
        for colour in (GREY, AMBER, GREEN, PURPLE, SKY):
            assert colour not in out


class TestActivePill:
    """window-status-current-format: bg highlight + glyph + colour per state."""

    def test_idle_bg_highlight_purple(self, server):
        srv, wins = server
        out = render(srv, wins["idle"], current=True)
        assert "○" in out
        assert "#313244" in out  # active bg highlight
        assert PURPLE in out

    def test_busy_bg_highlight_amber(self, server):
        srv, wins = server
        out = render(srv, wins["busy"], current=True)
        assert "◐" in out
        assert "#313244" in out
        assert AMBER in out

    def test_done_bg_highlight_green(self, server):
        srv, wins = server
        out = render(srv, wins["done"], current=True)
        assert "●" in out
        assert "#313244" in out
        assert GREEN in out

    def test_ask_bg_highlight_sky(self, server):
        srv, wins = server
        out = render(srv, wins["ask"], current=True)
        assert "?" in out
        assert "#313244" in out
        assert SKY in out
        assert out.count("#[") == 2  # bg block + fg block, not truncated

    def test_active_not_truncated(self, server):
        """The split-style fix: the pill is not cut off at the bg comma."""
        srv, wins = server
        out = render(srv, wins["busy"], current=True)
        # A truncated pill would end right after "#[bg=#313244" with no glyph.
        assert "◐" in out
        assert out.endswith(" ")  # trailing space kept, pill not cut short
        assert out.count("#[") == 2  # exactly bg block + fg block

    def test_unset_falls_back_to_classic(self, server):
        srv, wins = server
        out = render(srv, wins["unset"], current=True)
        # Classic active is a bare " ● " (no bg highlight, no inline colour).
        assert "●" in out
        assert "#313244" not in out
        assert "#[" not in out


class TestConfLoads:
    """The shipped conf parses without error (syntactic smoke test)."""

    def test_conf_has_no_parse_errors(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        srv = TmuxServer(tmp_path / "s.sock", home, TMUX_CONF)
        srv.kill()
        result = subprocess.run(
            [
                "tmux",
                "-S",
                str(tmp_path / "s.sock"),
                "-f",
                str(TMUX_CONF),
                "new-session",
                "-d",
                "-s",
                "t",
            ],
            capture_output=True,
            text=True,
            env=srv._env(),
            check=False,
        )
        time.sleep(0.1)
        srv.kill()
        assert result.returncode == 0, result.stderr
        # tmux prints config errors to stderr even when the session starts.
        assert "error" not in result.stderr.lower(), result.stderr
