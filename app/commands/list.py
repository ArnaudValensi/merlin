#!/usr/bin/env python3
"""List apps with their status (starting, running, exited) as JSON.

Examples:
  merlin app list
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import cli_support, sessions  # noqa: E402


def main() -> int:
    cli_support.parser("list", __doc__).parse_args()
    cli_support.emit([sessions.public(r) for r in sessions.list_sessions()])
    return 0


if __name__ == "__main__":
    cli_support.run(main)
