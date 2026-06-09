"""Argparse helper: show full help on a usage error.

`HelpfulParser` overrides `ArgumentParser.error` to print the parser's full
help (then the error) instead of just a one-line usage. Subparsers created by
a `HelpfulParser` inherit the class automatically, so a leaf command invoked
with missing required arguments (e.g. `merlin chat reply`) prints its own help
rather than a bare "the following arguments are required" line.
"""

from __future__ import annotations

import argparse
import sys


class HelpfulParser(argparse.ArgumentParser):
    """ArgumentParser that prints full help on a usage error, then exits 2."""

    def error(self, message: str):  # type: ignore[override]
        self.print_help(sys.stderr)
        self.exit(2, f"\n{self.prog}: error: {message}\n")
