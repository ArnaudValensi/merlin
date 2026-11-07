"""Workspace snapshot capture (workspace/snapshot.py): argv filtering and
building a snapshot from tmux rows and a fake process tree."""

import os
import time

from workspace import snapshot as snap

CONV = "11111111-2222-3333-4444-555555555555"


class FakeProc(snap.Proc):
    """A process tree: pid -> (ppid, argv, cwd)."""

    def __init__(self, procs: dict[int, tuple[int, list[str], str]]):
        self.procs = procs

    def argv(self, pid):
        return list(self.procs[pid][1]) if pid in self.procs else []

    def stat(self, pid):
        if pid not in self.procs:
            return None
        return ["S", str(self.procs[pid][0])] + ["0"] * 20

    def cwd(self, pid):
        return self.procs[pid][2] if pid in self.procs else None

    def children(self):
        out: dict[int, list[int]] = {}
        for pid, (ppid, _, _) in self.procs.items():
            out.setdefault(ppid, []).append(pid)
        return out


def window(session="main", wid="@1", index="0", name="w", provider="", conv="", **opts):
    row = {
        "session_name": session,
        "window_id": wid,
        "window_index": index,
        "window_name": name,
        "window_layout": "abcd,120x40,0,0,1",
        "window_active": "1",
        "automatic-rename": "0",
        "@agent_provider": provider,
        "@agent_conv": conv,
    }
    for key in snap.WINDOW_OPTIONS:
        row[key] = opts.get(key.removeprefix("@agent_"), "")
    return row


def pane(wid="@1", index="0", pid="100", cwd="/work", cmd="zsh"):
    return {
        "window_id": wid,
        "pane_index": index,
        "pane_pid": pid,
        "pane_active": "1",
        "pane_current_path": cwd,
        "pane_current_command": cmd,
    }


class TestArgv:
    def test_claude_keeps_options_drops_prompt_and_conversation(self):
        argv = [
            "claude",
            "--model",
            "opus",
            "--resume",
            CONV,
            "--dangerously-skip-permissions",
            "fix the bug",
        ]
        assert snap.claude_resume_args(argv) == [
            "--model",
            "opus",
            "--dangerously-skip-permissions",
        ]

    def test_claude_print_mode_is_not_interactive(self):
        assert snap.claude_resume_args(["claude", "-p", "hello"]) is None

    def test_codex_tui_options(self):
        opts, sub, pos = snap.codex_args(["--yolo", "-m", "gpt", "resume", CONV])
        assert opts == ["--yolo", "-m", "gpt"]
        assert sub == "resume"
        assert pos == [CONV]

    def test_codex_exec_is_not_a_tui(self):
        assert snap.codex_args(["exec", "do it"]) is None

    def test_resume_commands(self):
        assert snap.resume_command(
            {"provider": "claude", "conv": "c1", "options": ["--model", "x"]}
        ) == [
            "claude",
            "--model",
            "x",
            "--resume",
            "c1",
        ]
        assert snap.resume_command(
            {"provider": "codex", "conv": "t1", "options": ["--yolo"]}
        ) == [
            "codex",
            "--yolo",
            "resume",
            "t1",
        ]


class TestBuild:
    def test_plain_shell_pane_has_no_agent(self):
        proc = FakeProc({100: (1, ["zsh"], "/work")})
        s = snap.build_snapshot(
            [{"session_name": "main"}], [window()], [pane()], proc, {}, now=1.0
        )
        p = s["sessions"][0]["windows"][0]["panes"][0]
        assert p == {"index": 0, "cwd": "/work", "active": True}

    def test_program_other_than_an_agent_is_not_recorded(self):
        proc = FakeProc(
            {100: (1, ["zsh"], "/work"), 101: (100, ["npm", "run", "dev"], "/work")}
        )
        s = snap.build_snapshot(
            [{"session_name": "main"}], [window()], [pane(cmd="npm")], proc, {}
        )
        assert "agent" not in s["sessions"][0]["windows"][0]["panes"][0]

    def test_claude_from_session_file(self):
        proc = FakeProc(
            {
                100: (1, ["zsh"], "/work"),
                101: (100, ["claude", "--effort", "high"], "/elsewhere"),
            }
        )
        records = {
            101: {
                "pid": 101,
                "sessionId": CONV,
                "cwd": "/work/proj",
                "kind": "interactive",
            }
        }
        s = snap.build_snapshot(
            [{"session_name": "main"}], [window()], [pane()], proc, records
        )
        assert s["sessions"][0]["windows"][0]["panes"][0]["agent"] == {
            "provider": "claude",
            "conv": CONV,
            "cwd": "/work/proj",
            "options": ["--effort", "high"],
        }

    def test_claude_falls_back_to_window_conv(self):
        proc = FakeProc({100: (1, ["zsh"], "/work"), 101: (100, ["claude"], "/work/p")})
        w = window(provider="claude", conv="conv-from-hook")
        s = snap.build_snapshot([{"session_name": "main"}], [w], [pane()], proc, {})
        agent = s["sessions"][0]["windows"][0]["panes"][0]["agent"]
        assert agent["conv"] == "conv-from-hook"
        assert agent["cwd"] == "/work/p"

    def test_codex_from_window_conv(self):
        proc = FakeProc(
            {
                100: (1, ["zsh"], "/work"),
                101: (100, ["node", "/usr/bin/codex", "--yolo"], "/work/c"),
            }
        )
        w = window(provider="codex", conv="thread-1")
        s = snap.build_snapshot([{"session_name": "main"}], [w], [pane()], proc, {})
        assert s["sessions"][0]["windows"][0]["panes"][0]["agent"] == {
            "provider": "codex",
            "conv": "thread-1",
            "cwd": "/work/c",
            "options": ["--yolo"],
        }

    def test_codex_from_resume_argv(self):
        proc = FakeProc(
            {100: (1, ["zsh"], "/w"), 101: (100, ["codex", "resume", CONV], "/w")}
        )
        s = snap.build_snapshot(
            [{"session_name": "main"}], [window()], [pane()], proc, {}
        )
        assert s["sessions"][0]["windows"][0]["panes"][0]["agent"]["conv"] == CONV

    def test_unidentified_codex_is_a_plain_pane(self):
        proc = FakeProc({100: (1, ["zsh"], "/w"), 101: (100, ["codex"], "/w")})
        s = snap.build_snapshot(
            [{"session_name": "main"}], [window()], [pane()], proc, {}
        )
        assert "agent" not in s["sessions"][0]["windows"][0]["panes"][0]

    def test_window_conv_of_other_provider_is_not_used(self):
        proc = FakeProc({100: (1, ["zsh"], "/w"), 101: (100, ["codex"], "/w")})
        w = window(provider="claude", conv="claude-conv")
        s = snap.build_snapshot([{"session_name": "main"}], [w], [pane()], proc, {})
        assert "agent" not in s["sessions"][0]["windows"][0]["panes"][0]

    def test_layout_order_options_and_active_window(self):
        proc = FakeProc(
            {100: (1, ["zsh"], "/a"), 200: (1, ["zsh"], "/b"), 201: (1, ["zsh"], "/c")}
        )
        w1 = window(wid="@1", index="3", name="three", sid="s-3", cwd="/a")
        w1["window_active"] = "0"
        w2 = window(wid="@2", index="1", name="one", parent="s-3", relation="child")
        rows = [
            pane("@1", "0", "100", "/a"),
            pane("@2", "1", "201", "/c"),
            pane("@2", "0", "200", "/b"),
        ]
        s = snap.build_snapshot([{"session_name": "main"}], [w1, w2], rows, proc, {})
        sess = s["sessions"][0]
        assert [w["index"] for w in sess["windows"]] == [1, 3]
        assert sess["active_window"] == 1
        assert [p["cwd"] for p in sess["windows"][0]["panes"]] == ["/b", "/c"]
        assert sess["windows"][1]["options"] == {
            "@agent_sid": "s-3",
            "@agent_cwd": "/a",
        }
        assert sess["windows"][0]["options"] == {
            "@agent_parent": "s-3",
            "@agent_relation": "child",
        }

    def test_same_content_ignores_saved_at(self):
        a = {"version": 1, "saved_at": 1.0, "sessions": [{"name": "x", "windows": []}]}
        b = dict(a, saved_at=2.0)
        c = dict(a, sessions=[])
        assert snap.same_content(a, b)
        assert not snap.same_content(a, c)
        assert not snap.same_content(a, None)


class TestServerIdentity:
    def test_recorded_from_the_session_rows(self):
        proc = FakeProc({100: (1, ["zsh"], "/work")})
        rows = [{"session_name": "main", "pid": "4242", "start_time": "1791200000"}]
        s = snap.build_snapshot(rows, [window()], [pane()], proc, {}, now=1.0)
        assert s["server"] == {"pid": 4242, "start_time": 1791200000}

    def test_absent_when_the_rows_do_not_carry_it(self):
        proc = FakeProc({100: (1, ["zsh"], "/work")})
        s = snap.build_snapshot(
            [{"session_name": "main"}], [window()], [pane()], proc, {}, now=1.0
        )
        assert "server" not in s
        assert snap.server_identity([{"session_name": "m", "pid": "x"}]) is None
        assert snap.server_identity([]) is None

    def test_the_sessions_read_asks_for_it(self, monkeypatch):
        asked = []

        def rows(args, fields):
            asked.append((tuple(args), fields))
            return [], snap.OK

        monkeypatch.setattr(snap, "_list", rows)
        snap.read_tmux()
        assert asked[0] == (("list-sessions",), ("session_name", "pid", "start_time"))


def rows_of(pid, *sessions, start="7"):
    """Canned session, window and pane rows all from one server, one window
    per session."""
    ident = {"pid": str(pid), "start_time": start}
    sessions = sessions or ("main",)
    return (
        [{"session_name": name, **ident} for name in sessions],
        [
            {**window(session=name, wid=f"@{n}"), **ident}
            for n, name in enumerate(sessions)
        ],
        [{**pane(wid=f"@{n}", pid="999999999"), **ident} for n in range(len(sessions))],
    )


class TestReadTmux:
    def test_a_read_across_two_servers_is_a_failed_read(self, monkeypatch):
        # list-sessions answered by server A, the windows and panes by the
        # replacement B: a snapshot of neither. FAILED leaves latest alone.
        a, b = rows_of(1), rows_of(2)
        results = iter([(a[0], snap.OK), (b[1], snap.OK), (b[2], snap.OK)])
        monkeypatch.setattr(snap, "_list", lambda args, fields: next(results))
        assert snap.read_tmux_status() == (None, snap.FAILED)
        results = iter([(b[0], snap.OK), (b[1], snap.OK), (b[2], snap.OK)])
        assert snap.read_tmux_status()[1] == snap.OK

    def test_the_old_workspace_survives_a_mixed_read(self, monkeypatch, tmp_path):
        # The sweep: a mixed read (FAILED, nothing written), then a consistent
        # read of the replacement: the old workspace is frozen as the offer.
        from workspace import service, store

        monkeypatch.setenv("MERLIN_HOME", str(tmp_path))
        a, b = rows_of(1, "main", "alpha"), rows_of(2, "main")
        rich = snap.build_snapshot(*a, FakeProc({}), {}, now=time.time() - 60)
        assert [s["name"] for s in rich["sessions"]] == ["main", "alpha"]
        store.write_snapshot(store.latest_path(), rich)
        state = service.SweepState(last=rich)
        answers = iter(
            [(a[0], snap.OK), (b[1], snap.OK), (b[2], snap.OK)]
            + [(b[0], snap.OK), (b[1], snap.OK), (b[2], snap.OK)]
        )
        monkeypatch.setattr(snap, "_list", lambda args, fields: next(answers))
        service.sweep_once(state)
        assert store.read_snapshot(store.latest_path()) == rich
        service.sweep_once(state)
        offer = store.read_pending()
        assert offer["crash"]["reason"] == "server-changed"
        assert [s["name"] for s in offer["sessions"]] == ["main", "alpha"]
        assert store.server_of(offer) == (1, 7)
        assert store.server_of(store.read_snapshot(store.latest_path())) == (2, 7)

    def test_a_failed_read_is_no_snapshot(self, monkeypatch):
        # A failed list-panes must not become a snapshot without panes, and
        # a server that answered list-sessions is present: a failed read,
        # never "no server".
        results = iter(
            [([{"session_name": "main"}], snap.OK), ([window()], snap.OK), (None, "x")]
        )
        monkeypatch.setattr(snap, "_list", lambda args, fields: next(results))
        assert snap.read_tmux_status() == (None, snap.FAILED)

    def test_failed_window_read_is_no_snapshot(self, monkeypatch):
        results = iter([([{"session_name": "main"}], snap.OK), (None, "x")])
        monkeypatch.setattr(snap, "_list", lambda args, fields: next(results))
        assert snap.read_tmux() is None

    def test_no_server_is_told_from_a_failed_read(self, monkeypatch, tmp_path):
        import subprocess

        class R:
            def __init__(self, rc, err):
                self.returncode, self.stdout, self.stderr = rc, "", err

        # A connect error is judged on the socket it names, never on the
        # errno text (localized, and EACCES says nothing about the server).
        absent = str(tmp_path / "gone")
        stale = str(tmp_path / "stale")
        live = str(tmp_path / "live")
        import socket

        with socket.socket(socket.AF_UNIX) as s:
            s.bind(stale)  # a socket file nobody accepts on any more
        server = socket.socket(socket.AF_UNIX)
        server.bind(live)
        server.listen(1)
        closed = tmp_path / "closed"  # a socket directory that cannot be read
        closed.mkdir()
        closed.chmod(0)
        answers = {
            "no server running on /tmp/tmux-1000/default": snap.NO_SERVER,
            "server exited unexpectedly": snap.NO_SERVER,
            f"error connecting to {absent} (No such file or directory)": snap.NO_SERVER,
            f"error connecting to {stale} (Connection refused)": snap.NO_SERVER,
            f"error connecting to {absent} (Aucun fichier ou dossier)": snap.NO_SERVER,
            f"error connecting to {tmp_path}/nodir/x (No such file)": snap.NO_SERVER,
            f"error connecting to {live} (Permission denied)": snap.FAILED,
            f"error connecting to {live} (Connection timed out)": snap.FAILED,
            "merlin tests: tmux refused (list-sessions)": snap.FAILED,
            "lost server": snap.FAILED,
        }
        if os.getuid() != 0:  # root reads a mode-0 directory
            answers[f"error connecting to {closed}/default (Permission denied)"] = (
                snap.FAILED
            )
        try:
            for err, status in answers.items():
                monkeypatch.setattr(snap.subprocess, "run", lambda *a, **k: R(1, err))
                assert snap.read_tmux_status() == (None, status), err
        finally:
            server.close()
            closed.chmod(0o700)

        def timeout(*a, **k):
            raise subprocess.TimeoutExpired("tmux", 5)

        monkeypatch.setattr(snap.subprocess, "run", timeout)
        assert snap.read_tmux_status() == (None, snap.FAILED)


class TestKeptJobWindows:
    def _build(self, sessions, windows, panes):
        proc = FakeProc({100: (1, ["zsh"], "/work")})
        return snap.build_snapshot(sessions, windows, panes, proc, {}, now=1.0)

    def _job_window(self, wid, index, **options):
        return {
            **window(),
            "session_name": "merlin-jobs",
            "window_id": wid,
            "window_index": str(index),
            "window_name": wid.strip("@"),
            "window_active": "1",
            **options,
        }

    def _job_pane(self, wid):
        return pane(wid=wid)

    def test_a_run_stays_out_and_a_kept_window_comes_back_on_its_own(self):
        s = self._build(
            [{"session_name": "merlin-jobs"}],
            [
                self._job_window("@1", 1),
                self._job_window("@2", 2, **{"@job_stepped_in": "1"}),
                self._job_window("@3", 3, **{"@job_keep_window": "1"}),
            ],
            [self._job_pane("@1"), self._job_pane("@2"), self._job_pane("@3")],
        )
        assert [x["name"] for x in s["sessions"]] == ["merlin-jobs-kept"]
        kept = s["sessions"][0]
        assert [w["name"] for w in kept["windows"]] == ["2", "3"]
        assert [w["index"] for w in kept["windows"]] == [1, 2]
        assert kept["active_window"] is None

    def test_a_kept_window_records_its_run(self):
        s = self._build(
            [{"session_name": "merlin-jobs"}],
            [self._job_window("@2", 2, **{"@job_keep_window": "1", "@job_run": "r9"})],
            [self._job_pane("@2")],
        )
        assert s["sessions"][0]["windows"][0]["job_run"] == "r9"

    def test_a_codex_job_stamped_at_launch_resumes_its_conversation(self):
        """A hook in Codex's daemon never stamps the provider: the launch does."""
        proc = FakeProc(
            {
                100: (1, ["bash", "run.sh"], "/work"),
                101: (
                    100,
                    ["codex", "--dangerously-bypass-approvals-and-sandbox", "[Job: x]"],
                    "/work",
                ),
            }
        )
        s = snap.build_snapshot(
            [{"session_name": "merlin-jobs"}],
            [
                self._job_window(
                    "@2",
                    2,
                    **{
                        "@job_keep_window": "1",
                        "@agent_provider": "codex",
                        "@agent_conv": "conv-1",
                    },
                )
            ],
            [self._job_pane("@2")],
            proc,
            {},
            now=1.0,
        )
        agent = s["sessions"][0]["windows"][0]["panes"][0]["agent"]
        assert agent["provider"] == "codex" and agent["conv"] == "conv-1"
        assert "--dangerously-bypass-approvals-and-sandbox" in agent["options"]

    def test_no_kept_window_no_session(self):
        s = self._build(
            [{"session_name": "merlin-jobs"}],
            [self._job_window("@1", 1)],
            [self._job_pane("@1")],
        )
        assert s["sessions"] == []

    def test_they_join_a_kept_session_already_there(self):
        """Restored earlier, still open, and one more stepped into since."""
        s = self._build(
            [{"session_name": "merlin-jobs-kept"}, {"session_name": "merlin-jobs"}],
            [
                {**window(), "session_name": "merlin-jobs-kept", "window_index": "1"},
                self._job_window("@2", 5, **{"@job_stepped_in": "1"}),
            ],
            [pane(), self._job_pane("@2")],
        )
        assert [x["name"] for x in s["sessions"]] == ["merlin-jobs-kept"]
        assert [w["index"] for w in s["sessions"][0]["windows"]] == [1, 2]

    def test_a_claude_job_resumes_without_its_prompt(self):
        options = snap.claude_resume_args(
            ["claude", "--dangerously-skip-permissions", "[Job: x]\ndo it"]
        )
        assert options == ["--dangerously-skip-permissions"]

    def test_a_codex_job_resumes_without_its_prompt(self):
        options = snap.codex_args(
            ["--dangerously-bypass-approvals-and-sandbox", "[Job: x]\ndo it"]
        )
        assert "[Job: x]\ndo it" not in str(options)
        assert "--dangerously-bypass-approvals-and-sandbox" in str(options)
