"""Tests for the per-client session-switch wiring on the terminal WebSocket
(terminal/routes.py): tty resolution, the switch flow, and the control frame
the browser intercepts. The tmux calls are stubbed; the frame format is real."""

from __future__ import annotations

import asyncio
import json
import os
import pty
import time

import terminal.routes as tr


class FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def test_client_tty_resolves_a_pts():
    pid, fd = pty.fork()
    if pid == 0:
        time.sleep(2)
        os._exit(0)
    try:
        time.sleep(0.2)
        assert tr._client_tty(pid).startswith("/dev/pts/")
    finally:
        os.close(fd)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


def test_client_tty_missing_process_is_empty():
    # A pid with no /proc entry yields "" (switch then no-ops), never raises.
    assert tr._client_tty(2_147_483_000) == ""


def test_switch_session_switches_and_reports(monkeypatch):
    calls = {}
    monkeypatch.setattr(tr, "_client_tty", lambda pid: "/dev/pts/9")
    monkeypatch.setattr(
        tr.board_sweep,
        "switch_client",
        lambda tty, target: calls.setdefault("switch", (tty, target)) or True,
    )
    monkeypatch.setattr(tr.board_sweep, "client_session", lambda tty: "beta")

    ws = FakeWS()
    asyncio.run(tr._switch_session(ws, 123, "beta"))

    assert calls["switch"] == ("/dev/pts/9", "beta")
    assert len(ws.sent) == 1
    assert ws.sent[0][0] == "\x00"  # NUL-prefixed control frame
    assert json.loads(ws.sent[0][1:]) == {"type": "session", "name": "beta"}


def test_switch_session_noop_without_target(monkeypatch):
    monkeypatch.setattr(tr, "_client_tty", lambda pid: "/dev/pts/9")
    ws = FakeWS()
    asyncio.run(tr._switch_session(ws, 123, ""))
    assert ws.sent == []


def test_switch_session_noop_without_tty(monkeypatch):
    monkeypatch.setattr(tr, "_client_tty", lambda pid: "")
    ws = FakeWS()
    asyncio.run(tr._switch_session(ws, 123, "beta"))
    assert ws.sent == []


def test_report_current_session_frame(monkeypatch):
    monkeypatch.setattr(tr, "_client_tty", lambda pid: "/dev/pts/9")
    monkeypatch.setattr(tr.board_sweep, "client_session", lambda tty: "alpha")
    ws = FakeWS()
    asyncio.run(tr._report_current_session(ws, 1))
    assert json.loads(ws.sent[0][1:]) == {"type": "session", "name": "alpha"}
