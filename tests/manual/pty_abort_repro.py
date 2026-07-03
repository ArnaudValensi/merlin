"""Regression repro for the macOS PTY close/read kernel deadlock.

Before the PtyBridge fix (terminal/pty_bridge.py), a single abrupt
terminal WebSocket disconnect could freeze the whole Merlin process on
macOS: the cleanup's os.close() on the PTY master deadlocked in the
kernel against the executor thread's blocking os.read(), the process
entered uninterruptible sleep (state U, unkillable), the event loop
stopped, and the SaaS tunnel went zombie (2026-07-02 outage).

This script hammers that exact path: it opens the terminal WebSocket,
sends a keystroke so a PTY read is in flight, then aborts the TCP
connection with no close handshake, and verifies after every cycle that
the server still answers HTTP. Run it against a locally running Merlin,
primarily on macOS (Linux never deadlocked).

Not collected by pytest (manual test). Usage:

    uv run tests/manual/pty_abort_repro.py --password <dashboard-pass>
    uv run tests/manual/pty_abort_repro.py --url http://localhost:3201 \\
        --password <pass> --cycles 20

Exit code 0 = server survived all cycles; 1 = server stopped responding
(pre-fix behavior on macOS, usually within the first few cycles).
"""

import argparse
import asyncio
import os
import sys

import httpx
import websockets


def login(base_url: str, password: str) -> str:
    """POST /login and return the session cookie value."""
    resp = httpx.post(
        f"{base_url}/login",
        data={"password": password, "next": "/files"},
        follow_redirects=False,
        timeout=10,
    )
    cookie = resp.cookies.get("session")
    if resp.status_code != 303 or not cookie:
        print(f"Login failed (HTTP {resp.status_code}). Wrong password?")
        sys.exit(2)
    return cookie


def server_alive(base_url: str, timeout: float) -> bool:
    try:
        return httpx.get(f"{base_url}/login", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


async def abort_cycle(ws_url: str, cookie: str) -> None:
    """Connect, put a PTY read in flight, then abort without a close frame."""
    conn = await websockets.connect(
        ws_url,
        additional_headers={"Cookie": f"session={cookie}"},
        open_timeout=10,
    )
    try:
        # A keystroke guarantees PTY activity; wait for one output frame so
        # the server-side read loop is definitely engaged.
        await conn.send("q")
        try:
            await asyncio.wait_for(conn.recv(), timeout=3)
        except asyncio.TimeoutError:
            pass
    finally:
        # The whole point: TCP abort, no WebSocket close handshake.
        conn.transport.abort()


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url", default="http://localhost:3123", help="Merlin base URL"
    )
    parser.add_argument(
        "--password",
        default=os.getenv("DASHBOARD_PASS", ""),
        help="Dashboard password (default: $DASHBOARD_PASS)",
    )
    parser.add_argument("--cycles", type=int, default=12, help="Abort cycles to run")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    ws_url = base_url.replace("http", "ws", 1) + "/ws/terminal"
    cookie = login(base_url, args.password)

    for i in range(1, args.cycles + 1):
        await abort_cycle(ws_url, cookie)
        # Give the server a moment to run its cleanup path
        await asyncio.sleep(0.3)
        if not server_alive(base_url, timeout=5):
            print(f"FAIL: server stopped responding after cycle {i}/{args.cycles}")
            print("(pre-fix behavior: PTY close/read kernel deadlock)")
            return 1
        print(f"cycle {i}/{args.cycles}: server alive")

    print(f"PASS: server survived {args.cycles} abrupt disconnects")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
