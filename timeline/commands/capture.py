#!/usr/bin/env python3
"""Show or change Timeline's independent historical-capture consent."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


EXTENSION_DIR = Path(__file__).resolve().parents[1]
APP_DIR = EXTENSION_DIR.parent
sys.path.insert(0, str(APP_DIR))

from timeline.consent import capture_setting, set_capture_mode
from timeline.reconcile import sync_hooks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="merlin timeline capture",
        description=(
            "Show or set Timeline capture. This command remains available while "
            "the Timeline web extension is disabled."
        ),
    )
    parser.add_argument("mode", nargs="?", choices=("auto", "ask", "off"))
    args = parser.parse_args(argv)
    if args.mode is None:
        mode, source = capture_setting()
        print(f"timeline capture = {mode} ({source})")
        return 0

    mode = set_capture_mode(args.mode)
    status = sync_hooks()
    print(f"timeline capture = {mode} ({status})")
    return 1 if status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
