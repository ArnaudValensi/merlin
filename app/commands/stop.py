#!/usr/bin/env python3
"""Stop an app: end the app and its display, and forget it.

Examples:
  merlin app stop oob
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import cli_support, sessions  # noqa: E402


def main() -> int:
    parser = cli_support.parser("stop", __doc__)
    parser.add_argument("id", help="app id (see merlin app list)")
    args = parser.parse_args()
    cli_support.emit(sessions.public(sessions.stop(args.id)))
    return 0


if __name__ == "__main__":
    cli_support.run(main)
