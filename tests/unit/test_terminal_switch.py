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
    monkeypatch.setattr(
        tr.board_sweep,
        "client_session_info",
        lambda tty: tr.board_sweep.ClientSession("beta", "$2", 200),
    )

    ws = FakeWS()
    state = tr.SessionReportState()
    asyncio.run(tr._switch_session(ws, 123, "beta", state))

    assert calls["switch"] == ("/dev/pts/9", "beta")
    assert state.last_reported == tr.board_sweep.ClientSession("beta", "$2", 200)
    assert len(ws.sent) == 1
    assert ws.sent[0][0] == "\x00"  # NUL-prefixed control frame
    assert json.loads(ws.sent[0][1:]) == {
        "type": "session",
        "name": "beta",
        "id": "$2",
        "created": 200,
    }


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
    monkeypatch.setattr(
        tr.board_sweep,
        "client_session_info",
        lambda tty: tr.board_sweep.ClientSession("alpha", "$1", 100),
    )
    ws = FakeWS()
    asyncio.run(tr._report_current_session(ws, 1))
    assert json.loads(ws.sent[0][1:]) == {
        "type": "session",
        "name": "alpha",
        "id": "$1",
        "created": 100,
    }


def test_session_watcher_reports_tmux_native_switches_once(monkeypatch):
    """A client moved outside the panel still refreshes the browser pin."""
    alpha = tr.board_sweep.ClientSession("alpha", "$1", 100)
    beta = tr.board_sweep.ClientSession("beta", "$2", 200)
    reads = 0

    async def fake_read(_pid):
        nonlocal reads
        current = (alpha, alpha, beta)[min(reads, 2)]
        reads += 1
        return current

    monkeypatch.setattr(tr, "_read_current_session", fake_read)

    async def run():
        ws = FakeWS()
        task = asyncio.create_task(tr._watch_current_session(ws, 123, interval=0))
        try:
            for _ in range(20):
                if len(ws.sent) == 2:
                    break
                await asyncio.sleep(0)
            assert len(ws.sent) == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ws.sent

    sent = asyncio.run(run())
    frames = [json.loads(message[1:]) for message in sent]
    assert [frame["name"] for frame in frames] == ["alpha", "beta"]
    assert [frame["id"] for frame in frames] == ["$1", "$2"]


def test_session_watcher_does_not_repeat_panel_switch_frame(monkeypatch):
    """The watcher shares dedup state with the immediate panel response."""
    beta = tr.board_sweep.ClientSession("beta", "$2", 200)

    async def fake_read(_pid):
        return beta

    monkeypatch.setattr(tr, "_read_current_session", fake_read)

    async def run():
        ws = FakeWS()
        state = tr.SessionReportState()
        state.last_reported = beta
        task = asyncio.create_task(
            tr._watch_current_session(ws, 123, state, interval=0)
        )
        try:
            for _ in range(5):
                await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ws.sent

    assert asyncio.run(run()) == []
