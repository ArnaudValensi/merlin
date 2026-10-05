"""Workspace restore against a real, isolated tmux server.

Never the user's server: ``TMUX_TMPDIR`` points tmux's default socket at a
private directory, so Merlin's own bare ``tmux`` calls are isolated without any
code seam. ``claude`` / ``codex`` are stubs first on PATH that record their
argv and exit, so the pane falls back to its shell. Teardown kills only the
private socket.
"""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from workspace import restore as rs
from workspace import snapshot as snap

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)

STUB = """#!/bin/bash
printf '%s\\n' "$PWD" "$@" > "$WS_LOG/{name}.$(basename "$PWD").args"
"""


@pytest.fixture
def iso(monkeypatch, tmp_path):
    sockdir = Path(tempfile.mkdtemp(prefix="wsr-"))  # short: unix socket path limit
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "log"
    log.mkdir()
    for name in ("claude", "codex"):
        stub = bindir / name
        stub.write_text(STUB.format(name=name))
        stub.chmod(0o755)
    for var in ("TMUX", "TMUX_PANE", "CLAUDE_CONFIG_DIR", "MERLIN_SUPERVISED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(sockdir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("WS_LOG", str(log))
    monkeypatch.setattr(rs, "tmux_conf_args", lambda: [])
    monkeypatch.setattr(rs, "_LAUNCH_PAUSE", 0)
    yield tmp_path, log
    subprocess.run(
        ["tmux", "-S", str(sockdir / f"tmux-{os.getuid()}" / "default"), "kill-server"],
        capture_output=True,
        check=False,
    )
    shutil.rmtree(sockdir, ignore_errors=True)


def tmux(*args):
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=False
    ).stdout.strip()


def windows_of(session):
    out = tmux(
        "list-windows", "-t", f"={session}:", "-F", "#{window_index}\t#{window_name}"
    )
    return [tuple(line.split("\t")) for line in out.splitlines()]


def wait_for(path: Path, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and path.read_text().strip():
            return path.read_text().splitlines()
        time.sleep(0.05)
    raise AssertionError(f"{path} never written")


def dirs(root: Path, *names):
    out = []
    for n in names:
        d = root / n
        d.mkdir()
        out.append(str(d))
    return out


def snapshot_of(*sessions):
    return {"version": 1, "saved_at": 1.0, "sessions": list(sessions)}


def win(index, name, panes, layout="", **options):
    return {
        "index": index,
        "name": name,
        "automatic_rename": False,
        "layout": layout,
        "options": options,
        "panes": panes,
    }


class TestRestore:
    def test_rebuilds_sessions_and_resumes_agents(self, iso):
        root, log = iso
        proj, cdx, plain, other = dirs(root, "proj", "cdx", "plain", "other")
        main = {
            "name": "main",
            "active_window": 3,
            "windows": [
                win(
                    1,
                    "editor",
                    [
                        {"index": 0, "cwd": plain, "active": False},
                        {
                            "index": 1,
                            "cwd": plain,
                            "active": True,
                            "agent": {
                                "provider": "claude",
                                "conv": "conv-1",
                                "cwd": proj,
                                "options": ["--model", "opus"],
                            },
                        },
                    ],
                    **{"@agent_sid": "sid-1", "@agent_cwd": proj},
                ),
                win(
                    3,
                    "cx",
                    [
                        {
                            "index": 0,
                            "cwd": cdx,
                            "agent": {
                                "provider": "codex",
                                "conv": "thread-9",
                                "cwd": cdx,
                                "options": ["--yolo"],
                            },
                        }
                    ],
                ),
            ],
        }
        side = {
            "name": "side",
            "active_window": 0,
            "windows": [win(0, "sh", [{"index": 0, "cwd": other}])],
        }

        result = rs.restore(snapshot_of(main, side))

        assert result == {
            "restored": ["main", "side"],
            "skipped": [],
            "failed": [],
            "agents": 2,
        }
        assert windows_of("main") == [("1", "editor"), ("3", "cx")]
        assert windows_of("side") == [("0", "sh")]
        assert tmux("show-option", "-wv", "-t", "=main:1", "@agent_sid") == "sid-1"
        assert tmux("display-message", "-p", "-t", "=main:", "#{window_index}") == "3"
        cwds = tmux(
            "list-panes", "-t", "=main:1", "-F", "#{pane_current_path}"
        ).splitlines()
        assert cwds == [plain, proj]  # the agent pane starts where it must resume
        assert wait_for(log / "claude.proj.args") == [
            proj,
            "--model",
            "opus",
            "--resume",
            "conv-1",
        ]
        assert wait_for(log / "codex.cdx.args") == [cdx, "--yolo", "resume", "thread-9"]
        # Nothing is typed into a plain pane.
        assert sorted(p.name for p in log.iterdir()) == [
            "claude.proj.args",
            "codex.cdx.args",
        ]

    def test_live_session_with_work_is_skipped(self, iso):
        root, _ = iso
        (d,) = dirs(root, "d")
        tmux("new-session", "-d", "-s", "main", "-c", d)
        tmux("new-window", "-d", "-t", "=main:")
        snapshot = snapshot_of(
            {
                "name": "main",
                "active_window": 0,
                "windows": [win(0, "x", [{"index": 0, "cwd": d}])],
            }
        )
        assert rs.missing_sessions(snapshot, rs.live_sessions()) == []
        result = rs.restore(snapshot)
        assert result["skipped"] == ["main"]
        assert len(windows_of("main")) == 2

    def test_other_single_shell_session_is_never_filled(self, iso):
        # Only the terminal's own default session is fillable: a user's live
        # one-shell session of the same name as a snapshot session is kept.
        root, _ = iso
        (d,) = dirs(root, "d")
        tmux("new-session", "-d", "-s", "personal", "-c", d)
        time.sleep(0.3)
        before = tmux("display-message", "-p", "-t", "=personal:", "#{window_id}")
        snapshot = snapshot_of(
            {
                "name": "personal",
                "active_window": 0,
                "windows": [win(0, "old", [{"index": 0, "cwd": d}])],
            }
        )
        assert rs.live_sessions() == {"personal": False}
        assert rs.restore(snapshot)["skipped"] == ["personal"]
        assert tmux("list-windows", "-t", "=personal:", "-F", "#{window_id}") == before

    def test_original_window_kept_when_it_got_busy_during_restore(
        self, iso, monkeypatch
    ):
        # The user starts something in the placeholder shell while the restore
        # runs: that window must survive.
        root, _ = iso
        a, b = dirs(root, "a", "b")
        tmux("new-session", "-d", "-s", "merlin-dev", "-c", a)
        time.sleep(0.3)
        original = tmux("display-message", "-p", "-t", "=merlin-dev:", "#{window_id}")
        real_select = rs._select_active

        def user_starts_work(sess):
            tmux("send-keys", "-t", original, "sleep 60", "Enter")
            for _ in range(100):
                if (
                    tmux(
                        "display-message",
                        "-p",
                        "-t",
                        original,
                        "#{pane_current_command}",
                    )
                    == "sleep"
                ):
                    break
                time.sleep(0.05)
            real_select(sess)

        monkeypatch.setattr(rs, "_select_active", user_starts_work)
        snapshot = snapshot_of(
            {
                "name": "merlin-dev",
                "active_window": 0,
                "windows": [
                    win(0, "zero", [{"index": 0, "cwd": a}]),
                    win(1, "one", [{"index": 0, "cwd": b}]),
                ],
            }
        )
        assert rs.restore(snapshot)["restored"] == ["merlin-dev"]
        ids = tmux(
            "list-windows", "-t", "=merlin-dev:", "-F", "#{window_id}"
        ).splitlines()
        assert original in ids
        assert len(ids) == 3

    @pytest.mark.parametrize(
        "count, split, size",
        [(4, "-h", ("120", "10")), (6, "-v", ("120", "40")), (6, "-h", ("200", "50"))],
    )
    def test_multi_pane_layout_round_trip(self, iso, count, split, size):
        # A real tmux layout, captured, then restored on a fresh server: every
        # pane comes back with the same geometry and directory. (The layout
        # string itself embeds tmux's pane ids, which no restore can keep.)
        root, _ = iso
        d = dirs(root, *[f"p{i}" for i in range(count)])
        tmux("new-session", "-d", "-s", "lay", "-x", size[0], "-y", size[1], "-c", d[0])
        for i in range(1, count):
            tmux("split-window", "-d", split, "-t", "=lay:", "-c", d[i])
            tmux(
                "select-layout",
                "-t",
                "=lay:",
                "even-horizontal" if split == "-h" else "tiled",
            )
        time.sleep(0.3)
        fmt = (
            "#{pane_width}x#{pane_height}+#{pane_left}+#{pane_top} #{pane_current_path}"
        )
        before = sorted(tmux("list-panes", "-t", "=lay:", "-F", fmt).splitlines())
        captured = snap.take_snapshot()
        tmux("kill-server")
        time.sleep(0.2)
        result = rs.restore(captured)
        assert result["failed"] == []
        assert (
            sorted(tmux("list-panes", "-t", "=lay:", "-F", fmt).splitlines()) == before
        )
        assert len(before) == count

    def test_fills_the_auto_created_default_session(self, iso):
        # The web terminal opened before Restore created a bare `merlin-dev`.
        root, log = iso
        a, b = dirs(root, "a", "b")
        tmux("new-session", "-d", "-s", "merlin-dev", "-c", a)
        time.sleep(0.3)  # let the shell become the pane's foreground command
        snapshot = snapshot_of(
            {
                "name": "merlin-dev",
                "active_window": 1,
                "windows": [
                    win(0, "zero", [{"index": 0, "cwd": a}]),
                    win(
                        1,
                        "one",
                        [
                            {
                                "index": 0,
                                "cwd": b,
                                "agent": {
                                    "provider": "claude",
                                    "conv": "c-2",
                                    "cwd": b,
                                    "options": [],
                                },
                            }
                        ],
                    ),
                ],
            }
        )
        assert [
            s["name"] for s in rs.missing_sessions(snapshot, rs.live_sessions())
        ] == ["merlin-dev"]
        result = rs.restore(snapshot)
        assert result["restored"] == ["merlin-dev"]
        assert windows_of("merlin-dev") == [("0", "zero"), ("1", "one")]
        assert (
            tmux("display-message", "-p", "-t", "=merlin-dev:", "#{window_index}")
            == "1"
        )
        assert wait_for(log / "claude.b.args") == [b, "--resume", "c-2"]

    def test_running_claude_conversation_is_not_launched_twice(self, iso, monkeypatch):
        root, log = iso
        (d,) = dirs(root, "d")
        monkeypatch.setattr(rs, "_live_claude_convs", lambda: {"busy-conv"})
        snapshot = snapshot_of(
            {
                "name": "s",
                "active_window": 0,
                "windows": [
                    win(
                        0,
                        "w",
                        [
                            {
                                "index": 0,
                                "cwd": d,
                                "agent": {
                                    "provider": "claude",
                                    "conv": "busy-conv",
                                    "cwd": d,
                                    "options": [],
                                },
                            }
                        ],
                    )
                ],
            }
        )
        assert rs.restore(snapshot)["agents"] == 0
        time.sleep(0.5)
        assert list(log.iterdir()) == []

    def test_capture_round_trip(self, iso):
        root, _ = iso
        x, y = dirs(root, "x", "y")
        original = snapshot_of(
            {
                "name": "rt",
                "active_window": 2,
                "windows": [
                    win(0, "first", [{"index": 0, "cwd": x, "active": True}]),
                    win(
                        2,
                        "second",
                        [{"index": 0, "cwd": y, "active": True}],
                        **{"@agent_sid": "keep-me"},
                    ),
                ],
            }
        )
        rs.restore(original)
        captured = snap.take_snapshot()
        (sess,) = captured["sessions"]
        assert sess["name"] == "rt"
        assert sess["active_window"] == 2
        assert [(w["index"], w["name"]) for w in sess["windows"]] == [
            (0, "first"),
            (2, "second"),
        ]
        assert [w["panes"][0]["cwd"] for w in sess["windows"]] == [x, y]
        assert sess["windows"][1]["options"] == {"@agent_sid": "keep-me"}


def test_server_spawn_prefix(monkeypatch):
    from terminal import tmux as ttmux

    monkeypatch.setattr(ttmux.shutil, "which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setenv("MERLIN_SUPERVISED", "1")
    assert ttmux.server_spawn_prefix(server_running=False)[:3] == [
        "systemd-run",
        "--user",
        "--scope",
    ]
    assert ttmux.server_spawn_prefix(server_running=True) == []
    monkeypatch.delenv("MERLIN_SUPERVISED")
    assert ttmux.server_spawn_prefix(server_running=False) == []


def test_restore_creating_the_server_uses_the_prefix(iso, monkeypatch):
    # The restore asks for the scope only when it creates the server.
    root, _ = iso
    (d,) = dirs(root, "d")
    seen = []
    monkeypatch.setattr(
        rs, "server_spawn_prefix", lambda running: seen.append(running) or []
    )
    one = {
        "name": "s1",
        "active_window": 0,
        "windows": [win(0, "w", [{"index": 0, "cwd": d}])],
    }
    two = dict(one, name="s2")
    rs.restore(snapshot_of(one, two))
    assert seen == [False, True]
