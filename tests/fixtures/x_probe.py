#!/usr/bin/python3
"""A tiny X11 app for the app-streaming tests (system Python + python-xlib).

Fills its window with #00ff88 and a 50 px magenta (#ff00ff) square at the
top-left, appends every key and button event to the file named by
``X_PROBE_LOG`` (``keydown x``, ``keyup x``, ``btndown 1 120 80``, ...), and
dumps its environment as JSON to ``X_PROBE_ENV``.

    x_probe.py [--size WxH] [--exit-after SECONDS] [--exit-code N]
"""

import argparse
import json
import os
import sys
import time

from Xlib import X, XK, Xatom, display

BACKGROUND = 0x00FF88
SQUARE = 0xFF00FF

# keysym -> X keysym name (Escape, Right, x), the names xdotool takes.
KEYSYM_NAMES: dict[int, str] = {}
for _attr in dir(XK):
    if _attr.startswith("XK_"):
        KEYSYM_NAMES.setdefault(getattr(XK, _attr), _attr[3:])


def log(line: str) -> None:
    path = os.environ.get("X_PROBE_LOG")
    if path:
        with open(path, "a") as handle:
            handle.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", default="640x480")
    parser.add_argument("--exit-after", type=float)
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args()
    width, height = (int(v) for v in args.size.split("x"))

    env_path = os.environ.get("X_PROBE_ENV")
    if env_path:
        with open(env_path, "w") as handle:
            json.dump(dict(os.environ), handle)

    disp = display.Display()
    screen = disp.screen()
    window = screen.root.create_window(
        0,
        0,
        width,
        height,
        0,
        screen.root_depth,
        X.InputOutput,
        X.CopyFromParent,
        background_pixel=BACKGROUND,
        event_mask=(
            X.ExposureMask
            | X.KeyPressMask
            | X.KeyReleaseMask
            | X.ButtonPressMask
            | X.ButtonReleaseMask
            | X.StructureNotifyMask
        ),
    )
    window.set_wm_name("x-probe")
    window.change_property(
        disp.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [os.getpid()]
    )
    gc = window.create_gc(foreground=SQUARE)
    window.map()
    disp.flush()
    print(f"x_probe ready on {os.environ.get('DISPLAY')}", flush=True)

    deadline = time.monotonic() + args.exit_after if args.exit_after else None
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            print("x_probe exiting", flush=True)
            return args.exit_code
        if not disp.pending_events():
            time.sleep(0.01)
            continue
        event = disp.next_event()
        if event.type == X.Expose:
            window.fill_rectangle(gc, 0, 0, 50, 50)
            disp.flush()
        elif event.type in (X.KeyPress, X.KeyRelease):
            keysym = disp.keycode_to_keysym(event.detail, 0)
            name = KEYSYM_NAMES.get(keysym, str(keysym))
            log(f"{'keydown' if event.type == X.KeyPress else 'keyup'} {name}")
        elif event.type in (X.ButtonPress, X.ButtonRelease):
            kind = "btndown" if event.type == X.ButtonPress else "btnup"
            log(f"{kind} {event.detail} {event.event_x} {event.event_y}")


if __name__ == "__main__":
    sys.exit(main())
