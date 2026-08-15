#!/usr/bin/env python3
"""Fail-open Claude Code/Codex activity hook. Never writes to the agent TUI."""

from __future__ import annotations

import signal
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_DIR))

from timeline.hook_runtime import process_payload, read_stdin_payload


HOOK_DEADLINE_SECONDS = 2.0


class HookDeadline(RuntimeError):
    pass


def _deadline(_signum, _frame) -> None:
    raise HookDeadline("activity hook deadline reached")


def main() -> int:
    signal.signal(signal.SIGALRM, _deadline)
    signal.setitimer(signal.ITIMER_REAL, HOOK_DEADLINE_SECONDS)
    try:
        provider = sys.argv[1].lower() if len(sys.argv) > 1 else ""
        payload = read_stdin_payload()
        if payload is not None and provider in {"claude", "codex"}:
            process_payload(provider, payload)
        return 0
    except BaseException:
        return 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    raise SystemExit(main())
