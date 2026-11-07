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
        tmux(
            "-S",
            os.environ["TMUX_TMPDIR"] + f"/tmux-{os.getuid()}/default",
            "kill-server",
        )
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

    def test_a_session_failing_halfway_is_rolled_back_and_retried(
        self, iso, monkeypatch
    ):
        # The second window fails: the session this restore created is killed
        # (by its id), so it is still missing, the offer stays, and a retry
        # rebuilds it whole.
        from workspace import service, store

        root, _ = iso
        a, b = dirs(root, "a", "b")
        snapshot = snapshot_of(
            {
                "name": "half",
                "active_window": 0,
                "windows": [
                    win(0, "zero", [{"index": 0, "cwd": a}]),
                    win(1, "one", [{"index": 0, "cwd": b}]),
                ],
            }
        )
        real_fill = rs._fill_window

        def fill_then_fail(win, wid, live, launched):
            if win["name"] == "one":
                raise rs.RestoreError("split failed")
            return real_fill(win, wid, live, launched)

        monkeypatch.setattr(rs, "_fill_window", fill_then_fail)
        store.write_snapshot(store.pending_path(), snapshot)
        result = service.restore_pending()
        assert result["failed"] == ["half"] and result["restored"] == []
        assert rs.live_sessions() in (None, {})  # nothing half built is left
        assert store.pending_path().exists()
        info = service.pending_info()
        assert [s["name"] for s in info["sessions"]] == ["half"]

        monkeypatch.setattr(rs, "_fill_window", real_fill)
        result = service.restore_pending()
        assert result == {
            "restored": ["half"],
            "skipped": [],
            "failed": [],
            "agents": 0,
        }
        assert windows_of("half") == [("0", "zero"), ("1", "one")]
        assert not store.pending_path().exists()

    def test_a_placeholder_failing_halfway_is_a_placeholder_again(
        self, iso, monkeypatch
    ):
        # The web terminal's bare merlin-dev, half filled then failing: the
        # windows added go, the original window comes back to its index, the
        # session is fillable again and still offered.
        root, _ = iso
        a, b = dirs(root, "a", "b")
        tmux("new-session", "-d", "-s", "merlin-dev", "-c", a)
        time.sleep(0.3)
        original = tmux("display-message", "-p", "-t", "=merlin-dev:", "#{window_id}")
        snapshot = snapshot_of(
            {
                "name": "merlin-dev",
                "active_window": 1,
                "windows": [
                    win(0, "zero", [{"index": 0, "cwd": a}]),
                    win(1, "one", [{"index": 0, "cwd": b}]),
                ],
            }
        )
        real_fill = rs._fill_window

        def fill_then_fail(win, wid, live, launched):
            if win["name"] == "one":
                raise rs.RestoreError("split failed")
            return real_fill(win, wid, live, launched)

        monkeypatch.setattr(rs, "_fill_window", fill_then_fail)
        assert rs.restore(snapshot)["failed"] == ["merlin-dev"]
        assert tmux("list-windows", "-t", "=merlin-dev:", "-F", "#{window_id}") == (
            original
        )
        assert tmux("display-message", "-p", "-t", original, "#{window_index}") == "0"
        assert rs.live_sessions() == {"merlin-dev": True}
        assert [
            s["name"] for s in rs.missing_sessions(snapshot, rs.live_sessions())
        ] == ["merlin-dev"]

        monkeypatch.setattr(rs, "_fill_window", real_fill)
        assert rs.restore(snapshot)["restored"] == ["merlin-dev"]
        assert windows_of("merlin-dev") == [("0", "zero"), ("1", "one")]

    def _replace_server(self, *sessions):
        """Kill the isolated server by its socket and start a replacement on the
        same socket, as the web terminal does the instant a server dies. The
        replacement hands out $0, @0, @1 again."""
        sock = os.environ["TMUX_TMPDIR"] + f"/tmux-{os.getuid()}/default"
        subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)
        for _ in range(100):  # the old server takes a moment to leave the socket
            r = subprocess.run(
                ["tmux", "-S", sock, "list-sessions"], capture_output=True, text=True
            )
            if "no server running" in r.stderr:
                break
            time.sleep(0.02)
        for name, windows in sessions:
            tmux("new-session", "-d", "-s", name)
            for _ in range(windows - 1):
                tmux("new-window", "-d", "-t", f"={name}:")
        time.sleep(0.3)

    def test_rollback_never_touches_a_replacement_server(self, iso, monkeypatch):
        # The server dies mid-restore and the web terminal recreates one on
        # the same socket, with the same ids, before the rollback runs. The
        # rollback is bound to the dead server: the replacement survives.
        root, _ = iso
        a, b = dirs(root, "a", "b")
        snapshot = snapshot_of(
            {
                "name": "half",
                "active_window": 0,
                "windows": [
                    win(0, "zero", [{"index": 0, "cwd": a}]),
                    win(1, "one", [{"index": 0, "cwd": b}]),
                ],
            }
        )
        real_fill = rs._fill_window

        def fill_then_replace(win, wid, live, launched):
            if win["name"] == "one":
                self._replace_server(("merlin-dev", 1))
                raise rs.RestoreError("server died")
            return real_fill(win, wid, live, launched)

        monkeypatch.setattr(rs, "_fill_window", fill_then_replace)
        assert rs.restore(snapshot)["failed"] == ["half"]
        assert tmux("list-sessions", "-F", "#{session_id} #{session_name}") == (
            "$0 merlin-dev"
        )

    def test_placeholder_rollback_never_touches_a_replacement_server(
        self, iso, monkeypatch
    ):
        root, _ = iso
        a, b = dirs(root, "a", "b")
        tmux("new-session", "-d", "-s", "merlin-dev", "-c", a)
        time.sleep(0.3)
        snapshot = snapshot_of(
            {
                "name": "merlin-dev",
                "active_window": 1,
                "windows": [
                    win(0, "zero", [{"index": 0, "cwd": a}]),
                    win(1, "one", [{"index": 0, "cwd": b}]),
                ],
            }
        )
        real_fill = rs._fill_window

        def fill_then_replace(win, wid, live, launched):
            if win["name"] == "one":
                # The replacement reuses @0 and @1, the ids the rollback holds
                # for the original and the added window, and index 0.
                self._replace_server(("other", 2))
                raise rs.RestoreError("server died")
            return real_fill(win, wid, live, launched)

        monkeypatch.setattr(rs, "_fill_window", fill_then_replace)
        assert rs.restore(snapshot)["failed"] == ["merlin-dev"]
        fmt = "#{session_name}:#{window_index} #{window_id}"
        after = tmux("list-windows", "-a", "-F", fmt)
        assert after.splitlines() == ["other:0 @0", "other:1 @1"]

    def test_running_claude_conversation_is_not_launched_twice(self, iso, monkeypatch):
        root, log = iso
        (d,) = dirs(root, "d")
        monkeypatch.setattr(
            rs, "live_conversations", lambda rows: {("claude", "busy-conv")}
        )
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

    def test_a_conversation_live_in_a_window_is_not_launched_twice(self, iso):
        # The hook stamps @agent_provider / @agent_conv on an agent's window:
        # a conversation named there is running, whatever the provider, and
        # its pane comes back as a plain shell. An unrelated one is launched.
        root, log = iso
        a, b, c = dirs(root, "a", "b", "c")
        tmux("new-session", "-d", "-s", "elsewhere", "-c", a)
        tmux("set-option", "-w", "-t", "=elsewhere:0", "@agent_provider", "codex")
        tmux("set-option", "-w", "-t", "=elsewhere:0", "@agent_conv", "thread-live")
        tmux("new-window", "-d", "-t", "=elsewhere:", "-c", a)
        tmux("set-option", "-w", "-t", "=elsewhere:1", "@agent_provider", "claude")
        tmux("set-option", "-w", "-t", "=elsewhere:1", "@agent_conv", "conv-live")
        assert rs.live_conversations(snap.read_tmux()) >= {
            ("codex", "thread-live"),
            ("claude", "conv-live"),
        }

        def agent(provider, conv, cwd):
            return {"provider": provider, "conv": conv, "cwd": cwd, "options": []}

        snapshot = snapshot_of(
            {
                "name": "s",
                "active_window": 0,
                "windows": [
                    win(
                        0,
                        "cx",
                        [
                            {
                                "index": 0,
                                "cwd": a,
                                "agent": agent("codex", "thread-live", a),
                            }
                        ],
                    ),
                    win(
                        1,
                        "cl",
                        [
                            {
                                "index": 0,
                                "cwd": b,
                                "agent": agent("claude", "conv-live", b),
                            }
                        ],
                    ),
                    win(
                        2,
                        "ok",
                        [
                            {
                                "index": 0,
                                "cwd": c,
                                "agent": agent("codex", "thread-new", c),
                            }
                        ],
                    ),
                ],
            }
        )
        result = rs.restore(snapshot)
        assert result["restored"] == ["s"] and result["agents"] == 1
        assert wait_for(log / "codex.c.args") == [c, "resume", "thread-new"]
        time.sleep(0.5)
        assert sorted(p.name for p in log.iterdir()) == ["codex.c.args"]

    def test_a_failed_tmux_read_stops_the_restore_before_anything(
        self, iso, monkeypatch
    ):
        # A Codex thread runs in a live window; the discovery read fails
        # (a timeout, say) while the later tmux calls would work. Nothing is
        # known about what is live, so nothing is created, nothing typed,
        # and the offer stays for a retry.
        from workspace import service, store

        root, log = iso
        a, b = dirs(root, "a", "b")
        tmux("new-session", "-d", "-s", "elsewhere", "-c", a)
        tmux("set-option", "-w", "-t", "=elsewhere:0", "@agent_provider", "codex")
        tmux("set-option", "-w", "-t", "=elsewhere:0", "@agent_conv", "thread-live")
        snapshot = snapshot_of(
            {
                "name": "recovered",
                "active_window": 0,
                "windows": [
                    win(
                        0,
                        "cx",
                        [
                            {
                                "index": 0,
                                "cwd": b,
                                "agent": {
                                    "provider": "codex",
                                    "conv": "thread-live",
                                    "cwd": b,
                                    "options": [],
                                },
                            }
                        ],
                    )
                ],
            }
        )
        store.write_pending(
            {**store.frozen(snapshot, "server-gone", 5.0), "id": "20200101-000001"}
        )
        with pytest.MonkeyPatch.context() as once:
            once.setattr(snap, "read_tmux_status", lambda: (None, snap.FAILED))
            with pytest.raises(rs.RestoreUnreadable, match="try again"):
                service.restore_pending()
        assert tmux("list-sessions", "-F", "#{session_name}") == "elsewhere"
        time.sleep(0.3)
        assert list(log.iterdir()) == []
        assert store.read_pending() is not None
        # The read works again: the thread is seen live and not launched.
        result = service.restore_pending()
        assert result["restored"] == ["recovered"] and result["agents"] == 0

    def test_a_conversation_named_twice_in_the_snapshot_is_typed_once(self, iso):
        # Codex is attributed per window, so a capture can name one thread
        # in several panes; the same for a Claude conversation seen through
        # the window option. One launch each, in the first pane, the others
        # plain shells; an unrelated conversation still launches.
        root, log = iso
        d = dirs(root, "p1", "p2", "p3", "p4", "p5", "p6")

        def agent(provider, conv, cwd):
            return {"provider": provider, "conv": conv, "cwd": cwd, "options": []}

        pane = lambda i, cwd, a=None: {
            "index": i,
            "cwd": cwd,
            **({"agent": a} if a else {}),
        }  # noqa: E731
        snapshot = snapshot_of(
            {
                "name": "one",
                "active_window": 0,
                "windows": [
                    win(
                        0,
                        "twice",
                        [
                            pane(0, d[0], agent("codex", "t-1", d[0])),
                            pane(1, d[1], agent("codex", "t-1", d[1])),
                        ],
                    ),
                    win(1, "again", [pane(0, d[2], agent("codex", "t-1", d[2]))]),
                    win(2, "cl", [pane(0, d[3], agent("claude", "c-1", d[3]))]),
                ],
            },
            {
                "name": "two",
                "active_window": 0,
                "windows": [
                    win(0, "cl2", [pane(0, d[4], agent("claude", "c-1", d[4]))]),
                    win(1, "new", [pane(0, d[5], agent("codex", "t-2", d[5]))]),
                ],
            },
        )
        result = rs.restore(snapshot)
        assert result["restored"] == ["one", "two"] and result["agents"] == 3
        assert wait_for(log / "codex.p1.args") == [d[0], "resume", "t-1"]
        assert wait_for(log / "claude.p4.args") == [d[3], "--resume", "c-1"]
        assert wait_for(log / "codex.p6.args") == [d[5], "resume", "t-2"]
        time.sleep(0.5)
        assert sorted(p.name for p in log.iterdir()) == [
            "claude.p4.args",
            "codex.p1.args",
            "codex.p6.args",
        ]

    def test_a_rolled_back_session_releases_what_it_typed(self, iso, monkeypatch):
        # Session "first" types thread X then fails and is rolled back (the
        # thread died with it); session "second" names X too and must get it.
        root, log = iso
        f, g, s = dirs(root, "f", "g", "s")
        real_fill = rs._fill_window

        def fill_then_fail(win, wid, live, launched):
            if win["name"] == "boom":
                raise rs.RestoreError("split failed")
            return real_fill(win, wid, live, launched)

        monkeypatch.setattr(rs, "_fill_window", fill_then_fail)
        agent = {"provider": "codex", "conv": "x", "cwd": "", "options": []}
        snapshot = snapshot_of(
            {
                "name": "first",
                "active_window": 0,
                "windows": [
                    win(
                        0, "ok", [{"index": 0, "cwd": f, "agent": {**agent, "cwd": f}}]
                    ),
                    win(1, "boom", [{"index": 0, "cwd": g}]),
                ],
            },
            {
                "name": "second",
                "active_window": 0,
                "windows": [
                    win(0, "ok", [{"index": 0, "cwd": s, "agent": {**agent, "cwd": s}}])
                ],
            },
        )
        result = rs.restore(snapshot)
        assert result["failed"] == ["first"] and result["restored"] == ["second"]
        assert result["agents"] == 1
        assert wait_for(log / "codex.s.args") == [s, "resume", "x"]

    def test_a_second_restore_fails_at_once_while_one_runs(self, iso, monkeypatch):
        import fcntl

        from workspace import service, store

        root, _ = iso
        (d,) = dirs(root, "d")
        snapshot = snapshot_of(
            {
                "name": "s",
                "active_window": 0,
                "windows": [win(0, "w", [{"index": 0, "cwd": d}])],
            }
        )
        store.write_pending(
            {**store.frozen(snapshot, "server-gone", 5.0), "id": "20200101-000001"}
        )
        rs.lock_path().parent.mkdir(parents=True, exist_ok=True)
        with open(rs.lock_path(), "a") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(rs.RestoreBusy, match="a restore is already running"):
                rs.restore(snapshot)
            with pytest.raises(rs.RestoreBusy):
                service.restore_pending()
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        assert store.read_pending() is not None  # nothing was consumed
        assert rs.live_sessions() in (None, {})  # nothing was created
        assert service.restore_pending()["restored"] == ["s"]
        assert store.read_pending() is None

    def test_the_restore_lock_holds_across_processes(self, iso):
        import sys

        root, _ = iso
        (d,) = dirs(root, "d")
        snapshot = snapshot_of(
            {
                "name": "s",
                "active_window": 0,
                "windows": [win(0, "w", [{"index": 0, "cwd": d}])],
            }
        )
        rs.lock_path().parent.mkdir(parents=True, exist_ok=True)
        # Another process (a CLI restore, say) holds the lock until told.
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, sys; f = open(sys.argv[1], 'a');"
                " fcntl.flock(f.fileno(), fcntl.LOCK_EX); print('held', flush=True);"
                " sys.stdin.readline()",
                str(rs.lock_path()),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "held"
            with pytest.raises(rs.RestoreBusy):
                rs.restore(snapshot)
        finally:
            holder.stdin.write("\n")
            holder.stdin.close()
            holder.wait(timeout=10)
        assert rs.restore(snapshot)["restored"] == ["s"]

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
        server = captured["server"]
        assert isinstance(server["pid"], int) and server["pid"] > 0
        assert isinstance(server["start_time"], int) and server["start_time"] > 0
        assert server == snap.server_identity(snap.read_tmux()[0])


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


class TestKeptJobWindows:
    """Saved under merlin-jobs-kept, but alive in merlin-jobs while tmux lives."""

    def _snapshot(self):
        return {
            "sessions": [
                {
                    "name": snap.KEPT_JOBS_SESSION,
                    "active_window": None,
                    "windows": [
                        {"index": 1, "name": "a", "job_run": "r1", "panes": []},
                        {"index": 2, "name": "b", "job_run": "r2", "panes": []},
                    ],
                }
            ]
        }

    def test_a_merlin_restart_offers_nothing(self):
        """tmux lived on: nothing went away, and restoring would duplicate."""
        live = {"merlin-jobs": False}
        assert rs.missing_sessions(self._snapshot(), live, {"r1", "r2"}) == []

    def test_a_reboot_offers_them_all(self):
        missing = rs.missing_sessions(self._snapshot(), {})
        assert [w["name"] for w in missing[0]["windows"]] == ["a", "b"]

    def test_only_the_windows_really_gone_come_back(self):
        missing = rs.missing_sessions(self._snapshot(), {"merlin-jobs": False}, {"r1"})
        assert [w["name"] for w in missing[0]["windows"]] == ["b"]

    def test_the_restore_itself_skips_a_live_run(self, monkeypatch):
        restored = []
        monkeypatch.setattr(snap, "read_tmux_status", lambda: (([], [], []), snap.OK))
        monkeypatch.setattr(rs, "live_sessions_of", lambda rows: {})
        monkeypatch.setattr(rs, "live_conversations", lambda rows: set())
        monkeypatch.setattr(rs, "job_runs_of", lambda rows: {"r1"})
        monkeypatch.setattr(
            rs,
            "_restore_new",
            lambda sess, *_: restored.extend(w["name"] for w in sess["windows"]) or 0,
        )
        monkeypatch.setattr(rs, "_select_active", lambda sess: None)
        result = rs.restore(self._snapshot())
        assert restored == ["b"]
        assert result["restored"] == [snap.KEPT_JOBS_SESSION]
