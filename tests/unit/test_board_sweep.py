"""Tests for the tmux sweep parser (board/sweep.py). Pure string parsing."""

import subprocess

from board import sweep


def line(**over):
    fields = {
        "@agent_sid": "s1",
        "@agent_state": "busy",
        "@agent_cwd": "/home/u/proj",
        "@agent_parent": "",
        "@agent_relation": "",
        "session_name": "t",
        "window_id": "@1",
        "window_index": "1",
        "window_active": "0",
        "window_activity": "1700",
        "window_name": "claude",
    }
    fields.update(over)
    return "\t".join(str(fields[f]) for f in sweep._FIELDS)


def sline(**over):
    fields = {
        "session_name": "alpha",
        "session_id": "$1",
        "session_attached": "1",
        "session_windows": "3",
        "session_activity": "1700",
    }
    fields.update(over)
    return "\t".join(str(fields[f]) for f in sweep._SESSION_FIELDS)


class TestParseSweep:
    def test_parses_a_full_row(self):
        (w,) = sweep.parse_sweep(line())
        assert w.sid == "s1"
        assert w.state == "busy"
        assert w.cwd == "/home/u/proj"
        assert w.window_id == "@1"
        assert w.index == 1
        assert w.activity == 1700
        assert w.is_agent is True

    def test_window_index_parsed(self):
        (w,) = sweep.parse_sweep(line(window_index="7"))
        assert w.index == 7

    def test_non_numeric_index_defaults_zero(self):
        (w,) = sweep.parse_sweep(line(window_index="x"))
        assert w.index == 0

    def test_active_flag(self):
        (w,) = sweep.parse_sweep(line(window_active="1"))
        assert w.active is True

    def test_plain_window_is_not_an_agent(self):
        (w,) = sweep.parse_sweep(line(**{"@agent_state": ""}))
        assert w.is_agent is False
        assert w.state == ""

    def test_state_is_lowercased_and_trimmed(self):
        (w,) = sweep.parse_sweep(line(**{"@agent_state": " DONE "}))
        assert w.state == "done"

    def test_relation_is_lowercased(self):
        (w,) = sweep.parse_sweep(line(**{"@agent_relation": "Sibling"}))
        assert w.relation == "sibling"

    def test_multiple_rows(self):
        raw = line(**{"@agent_sid": "a"}) + "\n" + line(**{"@agent_sid": "b"})
        assert [w.sid for w in sweep.parse_sweep(raw)] == ["a", "b"]

    def test_blank_lines_skipped(self):
        raw = "\n" + line() + "\n\n"
        assert len(sweep.parse_sweep(raw)) == 1

    def test_wrong_field_count_skipped(self):
        assert sweep.parse_sweep("too\tfew\tfields") == []

    def test_non_numeric_activity_defaults_zero(self):
        (w,) = sweep.parse_sweep(line(window_activity=""))
        assert w.activity == 0

    def test_window_name_with_spaces_survives(self):
        (w,) = sweep.parse_sweep(line(window_name="my long name"))
        assert w.name == "my long name"

    def test_empty_input(self):
        assert sweep.parse_sweep("") == []


class TestParseSessions:
    def test_parses_a_session_row(self):
        (s,) = sweep.parse_sessions(sline())
        assert s.name == "alpha"
        assert s.session_id == "$1"
        assert s.attached is True
        assert s.windows == 3
        assert s.activity == 1700

    def test_detached_session(self):
        (s,) = sweep.parse_sessions(sline(session_attached="0"))
        assert s.attached is False

    def test_multiple_sessions(self):
        raw = sline(session_name="a") + "\n" + sline(session_name="b")
        assert [s.name for s in sweep.parse_sessions(raw)] == ["a", "b"]

    def test_wrong_field_count_skipped(self):
        assert sweep.parse_sessions("only\ttwo") == []

    def test_name_with_spaces_survives(self):
        (s,) = sweep.parse_sessions(sline(session_name="my project"))
        assert s.name == "my project"

    def test_empty_input(self):
        assert sweep.parse_sessions("") == []


class TestSanitizeSessionName:
    def test_dots_and_colons_become_underscores(self):
        assert sweep.sanitize_session_name("v1.2:beta") == "v1_2_beta"

    def test_trims_whitespace(self):
        assert sweep.sanitize_session_name("  proj  ") == "proj"

    def test_empty_falls_back(self):
        assert sweep.sanitize_session_name("") == "session"
        assert sweep.sanitize_session_name("  ") == "session"


class TestCheckedSweep:
    def test_missing_tmux_is_unknown_but_board_remains_empty(self, monkeypatch):
        monkeypatch.setattr(sweep.shutil, "which", lambda _name: None)

        assert sweep.run_sweep_checked() is None
        assert sweep.run_sweep() == []

    def test_failed_tmux_is_unknown(self, monkeypatch):
        monkeypatch.setattr(sweep.shutil, "which", lambda _name: "/usr/bin/tmux")
        monkeypatch.setattr(
            sweep.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args, 1, "", "no server"
            ),
        )

        assert sweep.run_sweep_checked() is None

    def test_successful_empty_tmux_is_known_empty(self, monkeypatch):
        monkeypatch.setattr(sweep.shutil, "which", lambda _name: "/usr/bin/tmux")
        monkeypatch.setattr(
            sweep.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
        )

        assert sweep.run_sweep_checked() == []
