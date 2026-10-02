#!/usr/bin/env python3
"""Send keys, text, clicks or pointer moves to an app.

Keys are X keysym names (Right, Left, Up, Down, Return, Escape, Tab, space,
x, ctrl+z, ...). Coordinates are display pixels from the top-left corner (a
screenshot has the same size as the display).

Examples:
  merlin app input oob key Right
  merlin app input oob key Right --repeat 3 --delay 150
  merlin app input oob key x z ctrl+s
  merlin app input oob key Right --hold 80       # games that poll key state
  merlin app input oob type "hello world"
  merlin app input oob click 640 360
  merlin app input oob click 640 360 --button 3
  merlin app input oob move 100 100
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import cli_support, sessions  # noqa: E402


def main() -> int:
    parser = cli_support.parser("input", __doc__)
    parser.add_argument("id", help="app id (see merlin app list)")
    actions = parser.add_subparsers(dest="action", required=True)

    key = actions.add_parser("key", help="press and release keys in order")
    key.add_argument("keys", nargs="+", help="X keysym names, e.g. Right x ctrl+z")
    key.add_argument("--repeat", type=int, default=1, help="repeat the sequence")
    key.add_argument("--delay", type=int, default=50, help="ms between keys")
    key.add_argument(
        "--hold", type=int, default=0, help="ms to hold each key down (games)"
    )

    text = actions.add_parser("type", help="type text")
    text.add_argument("text")

    click = actions.add_parser("click", help="move the pointer and click")
    click.add_argument("x", type=int)
    click.add_argument("y", type=int)
    click.add_argument(
        "--button", type=int, default=1, help="1 left, 2 middle, 3 right"
    )

    move = actions.add_parser("move", help="move the pointer")
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)

    args = parser.parse_args()
    if args.action == "key":
        sessions.send_keys(
            args.id,
            args.keys,
            repeat=args.repeat,
            delay_ms=args.delay,
            hold_ms=args.hold,
        )
    elif args.action == "type":
        sessions.type_text(args.id, args.text)
    elif args.action == "click":
        sessions.click(args.id, args.x, args.y, args.button)
    else:
        sessions.move(args.id, args.x, args.y)
    cli_support.emit({"ok": True})
    return 0


if __name__ == "__main__":
    cli_support.run(main)
