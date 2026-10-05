"""Workspace snapshot capture (workspace/snapshot.py): argv filtering and
building a snapshot from tmux rows and a fake process tree."""

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


class TestReadTmux:
    def test_a_failed_read_is_no_snapshot(self, monkeypatch):
        # A failed list-panes must not become a snapshot without panes.
        results = iter([[{"session_name": "main"}], [window()], None])
        monkeypatch.setattr(snap, "_tmux_rows", lambda args, fields: next(results))
        assert snap.read_tmux() is None

    def test_failed_window_read_is_no_snapshot(self, monkeypatch):
        results = iter([[{"session_name": "main"}], None])
        monkeypatch.setattr(snap, "_tmux_rows", lambda args, fields: next(results))
        assert snap.read_tmux() is None
