"""``merlin workspace`` (workspace/cli.py) against canned files under a
temporary ``MERLIN_HOME`` and a real, isolated tmux server, Merlin not
running. The server is killed by its socket only."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from workspace import restore as rs
from workspace import store
from workspace.cli import main

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)

STUB = """#!/bin/bash
printf '%s\\n' "$PWD" "$@" > "$WS_LOG/{name}.$(basename "$PWD").args"
"""

T = time.time() - 3600


@pytest.fixture
def iso(monkeypatch, tmp_path):
    sockdir = Path(tempfile.mkdtemp(prefix="wsc-"))
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
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
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


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def session(name, *dirs, agent=None):
    panes = [{"index": i, "cwd": d, "active": i == 0} for i, d in enumerate(dirs)]
    if agent:
        panes[0]["agent"] = agent
    return {
        "name": name,
        "active_window": 1,
        "windows": [
            {
                "index": 1,
                "name": f"{name}-win",
                "automatic_rename": False,
                "layout": "",
                "options": {},
                "panes": panes,
            }
        ],
    }


def snapshot(saved_at, *sessions):
    return {"version": 1, "saved_at": saved_at, "sessions": list(sessions)}


@pytest.fixture
def canned(tmp_path):
    """Three history entries, two crash points (one pending), one too old."""
    d = str(tmp_path)
    h = [
        store.write_history(snapshot(T + n, session("alpha", d))) for n in (10, 20, 30)
    ]
    old = store.new_id(T - 40 * 86400)
    store.write_crash(
        old,
        store.frozen(
            snapshot(T - 40 * 86400, session("x", d)), "server-gone", T - 40 * 86400
        ),
    )
    c1 = store.new_id(T + 40)
    store.write_crash(
        c1, store.frozen(snapshot(T + 40, session("beta", d)), "startup", T + 41)
    )
    agent = {
        "provider": "codex",
        "conv": "thread-123456789",
        "cwd": d,
        "options": ["--yolo"],
    }
    point = store.frozen(
        snapshot(T + 50, session("dev", d, d, agent=agent), session("oob", d)),
        "server-changed",
        T + 51,
    )
    c2 = store.new_id(T + 50)
    store.write_crash(c2, point)
    store.write_pending({**point, "id": c2})
    return {"history": h, "old": old, "c1": c1, "c2": c2, "dir": d}


class TestList:
    def test_newest_first_with_kinds_counts_and_the_pending_marker(self, canned):
        code, out, _ = run("list")
        assert code == 0
        lines = out.splitlines()
        assert [line.split()[0] for line in lines] == [
            canned["c2"],
            canned["c1"],
            *reversed(canned["history"]),
        ]
        assert "crash server-changed" in lines[0] and "* pending offer" in lines[0]
        assert " 2 sessions   2 windows   1 agents" in lines[0]
        assert "crash startup" in lines[1] and "pending" not in lines[1]
        assert all("history" in line for line in lines[2:])
        assert canned["old"] not in out  # older than 30 days

    def test_all_shows_old_crashes_and_every_history_entry(self, canned, tmp_path):
        for n in range(25):
            store.write_history(snapshot(T - 600 - n, session("h", str(tmp_path))))
        code, out, _ = run("list")
        assert len([line for line in out.splitlines() if "history" in line]) == 20
        code, out, _ = run("list", "--all")
        assert len([line for line in out.splitlines() if "history" in line]) == 28
        assert canned["old"] in out

    def test_json(self, canned):
        code, out, _ = run("list", "--json")
        rows = json.loads(out)
        assert rows[0]["id"] == canned["c2"] and rows[0]["pending"] is True
        assert rows[0]["reason"] == "server-changed" and rows[0]["agents"] == 1
        assert rows[2]["kind"] == "history" and rows[2]["reason"] is None

    def test_empty_home(self):
        assert run("list") == (0, "no restore points yet\n", "")

    def test_a_foreign_entry_never_breaks_the_listing(self, canned, iso):
        # A version-1 file with a window missing its panes: not a restore
        # point. The others still list; show and restore refuse it cleanly.
        bad = store.history_dir() / "20261105-000000.json"
        bad.write_text(
            json.dumps(
                {
                    "version": 1,
                    "saved_at": T + 5,
                    "sessions": [{"name": "broken", "windows": [{}]}],
                }
            )
        )
        code, out, err = run("list")
        assert code == 0 and err == ""
        assert "20261105-000000" not in out and canned["c2"] in out
        code, out, err = run("show", "20261105-000000")
        assert code == 1 and "no restore point 20261105-000000" in err
        code, out, err = run("restore", "20261105-000000")
        assert code == 1 and "no restore point 20261105-000000" in err
        assert tmux("list-sessions") == ""
        # An explicit null where a string or list is read with a default.
        agent = {
            "provider": "codex",
            "conv": "t",
            "cwd": canned["dir"],
            "options": None,
        }
        bad.write_text(
            json.dumps(snapshot(T + 5, session("nulls", canned["dir"], agent=agent)))
        )
        code, out, err = run("restore", "20261105-000000", "--dry-run")
        assert code == 1 and "no restore point 20261105-000000" in err
        assert "Traceback" not in err and "resume" not in out
        point = snapshot(T + 5, session("nulls", canned["dir"]))
        point["sessions"][0]["windows"][0]["layout"] = None
        bad.write_text(json.dumps(point))
        code, out, err = run("restore", "20261105-000000")
        assert code == 1 and "no restore point 20261105-000000" in err
        assert tmux("list-sessions") == ""


class TestShow:
    def test_text(self, canned, iso):
        tmux("new-session", "-d", "-s", "oob")
        code, out, _ = run("show", canned["c2"])
        assert code == 0
        assert out.splitlines()[0].startswith(f"{canned['c2']}  saved ")
        assert (
            "crash server-changed" in out and "this is the pending restore offer" in out
        )
        assert "session dev  (missing from tmux now)" in out
        assert "session oob  (live in tmux)" in out
        assert "  window 1 dev-win" in out
        assert f"    pane 0  {canned['dir']}  [codex thread-1 --yolo]" in out
        assert f"    pane 1  {canned['dir']}" in out

    def test_an_unreadable_tmux_shows_an_unknown_state(self, canned, iso, monkeypatch):
        from workspace import snapshot as snap

        monkeypatch.setattr(snap, "read_tmux_status", lambda: (None, snap.FAILED))
        code, out, _ = run("show", canned["c2"])
        assert code == 0
        assert "live state unknown: tmux could not be read, try again" in out
        assert "session dev  (unknown)" in out and "missing" not in out
        code, out, _ = run("show", canned["c2"], "--json")
        assert json.loads(out)["missing"] is None

    def test_json_and_unknown(self, canned, iso):
        code, out, _ = run("show", canned["c1"], "--json")
        data = json.loads(out)
        assert data["id"] == canned["c1"] and data["missing"] == ["beta"]
        code, out, err = run("show", "20200101-000000")
        assert code == 1 and "no restore point 20200101-000000" in err
        assert "merlin workspace list" in err


class TestRestore:
    def test_no_offer_is_an_error_with_a_hint(self, iso):
        code, out, err = run("restore")
        assert code == 1 and out == ""
        assert "no pending restore offer" in err and "merlin workspace list" in err

    def test_dry_run_prints_the_plan_and_touches_nothing(self, canned, iso):
        code, out, _ = run("restore", "--dry-run")
        assert code == 0
        assert "nothing is touched" in out
        assert "no tmux server: it would be started" in out
        assert "create session dev" in out and "create session oob" in out
        assert "      codex --yolo resume thread-123456789" in out
        assert tmux("list-sessions") == ""
        assert store.read_pending() is not None

    def test_dry_run_sees_what_is_live(self, canned, iso):
        tmux("new-session", "-d", "-s", "oob")
        tmux("new-window", "-d", "-t", "=oob:")
        tmux("new-session", "-d", "-s", "other")
        tmux("set-option", "-w", "-t", "=other:0", "@agent_provider", "codex")
        tmux("set-option", "-w", "-t", "=other:0", "@agent_conv", "thread-123456789")
        code, out, _ = run("restore", canned["c2"], "--dry-run")
        assert "skip session oob: live and in use" in out
        assert "create session dev" in out
        assert "already running elsewhere: plain shell" in out
        assert "resume thread" not in out

    def test_dry_run_refuses_an_unreadable_tmux(self, canned, iso):
        # The engine would refuse this restore: the plan must not promise it.
        from workspace import snapshot as snap

        tmux("new-session", "-d", "-s", "other")
        tmux("set-option", "-w", "-t", "=other:0", "@agent_provider", "codex")
        tmux("set-option", "-w", "-t", "=other:0", "@agent_conv", "thread-123456789")
        with pytest.MonkeyPatch.context() as once:
            once.setattr(snap, "read_tmux_status", lambda: (None, snap.FAILED))
            code, out, err = run("restore", "--dry-run")
        assert code == 1 and "tmux could not be read, no plan: try again" in err
        assert "create session" not in out and "resume thread" not in out
        assert tmux("list-sessions", "-F", "#{session_name}") == "other"
        assert store.read_pending() is not None

    def test_unknown_session_restores_nothing(self, canned, iso):
        code, out, err = run("restore", "--session", "dev", "--session", "nope")
        assert code == 1
        assert "unknown session(s): nope" in err and "dev, oob" in err
        assert tmux("list-sessions") == ""

    def test_session_limits_the_restore_and_keeps_the_offer(self, canned, iso):
        root, log = iso
        code, out, _ = run("restore", "--session", "oob")
        assert code == 0
        assert "restored 1 session(s), 0 agent(s) resumed: oob" in out
        assert tmux("list-sessions", "-F", "#{session_name}") == "oob"
        assert store.read_pending() is not None  # dev is still missing
        assert list(log.iterdir()) == []

    def test_the_offer_restored_whole_is_dropped(self, canned, iso):
        root, log = iso
        code, out, _ = run("restore")
        assert code == 0
        assert "restored 2 session(s), 1 agent(s) resumed: dev, oob" in out
        assert sorted(tmux("list-sessions", "-F", "#{session_name}").split()) == [
            "dev",
            "oob",
        ]
        assert store.read_pending() is None
        time.sleep(0.5)
        assert [p.name for p in log.iterdir()] == [
            f"codex.{Path(canned['dir']).name}.args"
        ]

    def test_the_offer_restored_in_two_steps_is_dropped_at_the_second(
        self, canned, iso
    ):
        assert run("restore", "--session", "dev")[0] == 0
        assert store.read_pending() is not None
        assert run("restore", "--session", "oob")[0] == 0
        assert store.read_pending() is None
        assert sorted(tmux("list-sessions", "-F", "#{session_name}").split()) == [
            "dev",
            "oob",
        ]

    def test_the_offer_is_dropped_when_the_rest_was_live_already(self, canned, iso):
        tmux("new-session", "-d", "-s", "oob")
        tmux("new-window", "-d", "-t", "=oob:")  # live, in use: never restored
        assert run("restore", "--session", "dev")[0] == 0
        assert store.read_pending() is None

    def test_the_offer_stays_when_the_live_state_is_unknown_after(
        self, canned, iso, monkeypatch
    ):
        from workspace import snapshot as snap

        real = snap.read_tmux_status
        calls = []

        def flaky():
            calls.append(1)
            # The engine's own read works; the read after the restore fails.
            return real() if len(calls) == 1 else (None, snap.FAILED)

        monkeypatch.setattr(snap, "read_tmux_status", flaky)
        code, out, _ = run("restore")
        assert code == 0 and "restored 2 session(s)" in out
        assert store.read_pending() is not None

    def test_an_older_point_by_id_keeps_the_offer(self, canned, iso):
        code, out, _ = run("restore", canned["c1"])
        assert code == 0 and "beta" in out
        assert store.read_pending() is not None

    def test_a_failed_session_exits_non_zero(self, canned, iso, monkeypatch):
        monkeypatch.setattr(
            rs,
            "restore",
            lambda s: {
                "restored": ["oob"],
                "skipped": [],
                "failed": ["dev"],
                "agents": 0,
            },
        )
        code, out, err = run("restore")
        assert code == 1 and "failed: dev" in err and "oob" in out
        assert store.read_pending() is not None

    def test_a_running_restore_makes_it_fail_at_once(self, canned, iso):
        import fcntl

        rs.lock_path().parent.mkdir(parents=True, exist_ok=True)
        with open(rs.lock_path(), "a") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out, err = run("restore")
        assert code == 1 and "a restore is already running" in err
        assert tmux("list-sessions") == ""


def test_the_command_runs_from_a_cold_process(canned, tmp_path):
    # Through cli.py, as `merlin workspace` does, with Merlin not running.
    env = {**os.environ, "MERLIN_HOME": os.environ["MERLIN_HOME"]}
    r = subprocess.run(
        [sys.executable, "cli.py", "workspace", "list"],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parent.parent.parent,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0].startswith(canned["c2"])
    r = subprocess.run(
        [sys.executable, "cli.py", "workspace", "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parent.parent.parent,
        timeout=60,
    )
    assert r.returncode == 0 and "merlin workspace" in r.stdout
