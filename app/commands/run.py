#!/usr/bin/env python3
"""Run a GUI app on a private display and print its handle as JSON.

The app starts detached on its own Xvfb display and keeps running after this
command returns (like tmux). Everything after `--` is the command to run.

Examples:
  merlin app run -- ./build/OutOfBody
  merlin app run --name oob --controls gamepad --keys A=x,B=z,X=r,Y=Tab,Start=Escape -- ./build/OutOfBody
  merlin app run --replace --name oob -- ./build/OutOfBody     # after a rebuild
  merlin app run --size 1920x1080 --gpu off -- glxgears

Output: {id, display, pid, size, gpu, status, url, ...}. Exit 1 if the app
exited during start-up (the JSON then carries log_tail).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import cli_support, sessions  # noqa: E402


def main() -> int:
    parser = cli_support.parser("run", __doc__)
    parser.add_argument("--name", help="app id/name (default: the command's name)")
    parser.add_argument("--cwd", help="working directory (default: current)")
    parser.add_argument(
        "--size", default="1280x720", help="display size WxH (default 1280x720)"
    )
    parser.add_argument(
        "--gpu",
        default="auto",
        choices=sessions.GPU_MODES,
        help="GPU rendering through VirtualGL (default auto)",
    )
    parser.add_argument(
        "--controls", choices=sessions.CONTROLS, help="touch controls the player shows"
    )
    parser.add_argument(
        "--keys", help="gamepad buttons to X keysyms, e.g. A=x,B=z,Start=Escape"
    )
    parser.add_argument(
        "--no-fill", action="store_true", help="keep the app window's own size"
    )
    parser.add_argument(
        "--replace", action="store_true", help="stop an app with the same id first"
    )
    parser.add_argument("command", nargs="*", help="the command to run, after --")
    args = parser.parse_args()
    if not args.command:
        parser.error("no command given (put it after --)")

    record = sessions.launch(
        args.command,
        name=args.name,
        cwd=args.cwd,
        size=sessions.parse_size(args.size),
        gpu=args.gpu,
        controls=args.controls,
        keys=sessions.parse_keys(args.keys),
        fill=not args.no_fill,
        replace=args.replace,
        origin=sessions.tmux_origin(),
    )
    out = sessions.public(record)
    if record["status"] == "exited":
        out["log_tail"] = sessions.read_log(record["id"], tail=20)
        cli_support.emit(out)
        return 1
    cli_support.emit(out)
    return 0


if __name__ == "__main__":
    cli_support.run(main)
