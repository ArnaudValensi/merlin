"""Merlin development task runner.

Usage: uv run scripts.py <command>
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def cmd_test(args):
    """Run unit and integration tests (~4s)."""
    result = subprocess.run(
        [
            "uv", "run", "pytest",
            "tests/unit/",
            "merlin-bot/tests/",
            "-v",
        ],
        cwd=ROOT,
    )
    sys.exit(result.returncode)


def cmd_test_e2e(args):
    """Run E2E tests with Playwright (~2min)."""
    result = subprocess.run(
        [
            "uv", "run", "--with", "playwright",
            "pytest",
            "tests/e2e/",
            "-v",
        ],
        cwd=ROOT,
    )
    sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(
        description="Merlin development task runner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", title="commands")

    sub.add_parser("test", help="Run unit tests (~4s)")
    sub.add_parser("test-e2e", help="Run E2E tests with Playwright (~2min)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    commands = {
        "test": cmd_test,
        "test-e2e": cmd_test_e2e,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
