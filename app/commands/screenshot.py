#!/usr/bin/env python3
"""Save a PNG of an app's whole display and print its path as JSON.

Read the PNG afterwards to see what the app shows.

Examples:
  merlin app screenshot oob
  merlin app screenshot oob -o /tmp/oob.png
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import cli_support, sessions  # noqa: E402


def main() -> int:
    parser = cli_support.parser("screenshot", __doc__)
    parser.add_argument("id", help="app id (see merlin app list)")
    parser.add_argument(
        "-o", "--output", help="PNG path (default: under the apps data dir)"
    )
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve() if args.output else None
    path = sessions.screenshot(args.id, output)
    cli_support.emit({"path": str(path)})
    return 0


if __name__ == "__main__":
    cli_support.run(main)
