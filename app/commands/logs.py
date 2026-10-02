#!/usr/bin/env python3
"""Print an app's output (stdout and stderr), also after it exited.

Examples:
  merlin app logs oob
  merlin app logs oob --tail 50
  merlin app logs oob --follow        # until the app exits
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import cli_support, sessions  # noqa: E402


def main() -> int:
    parser = cli_support.parser("logs", __doc__)
    parser.add_argument("id", help="app id (see merlin app list)")
    parser.add_argument("--tail", type=int, help="only the last N lines")
    parser.add_argument(
        "--follow", "-f", action="store_true", help="keep printing new output"
    )
    args = parser.parse_args()
    record = sessions.get(args.id)
    sys.stdout.write(sessions.read_log(record["id"], tail=args.tail))
    sys.stdout.flush()
    if not args.follow:
        return 0
    path = sessions.log_path(record["id"])
    offset = path.stat().st_size if path.exists() else 0
    while True:
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
        except OSError:
            chunk = b""
        if chunk:
            offset += len(chunk)
            sys.stdout.write(chunk.decode(errors="replace"))
            sys.stdout.flush()
        elif sessions.get(record["id"])["status"] == "exited":
            return 0
        time.sleep(0.3)


if __name__ == "__main__":
    cli_support.run(main)
