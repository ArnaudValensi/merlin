"""Merlin development task runner.

Usage: uv run scripts.py <command>
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    print(f"  → {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=ROOT)


def cmd_test(args):
    """Run unit and integration tests (~4s)."""
    sys.exit(
        run(
            ["uv", "run", "pytest", "tests/unit/", "merlin-bot/tests/", "-v"]
        ).returncode
    )


def cmd_test_e2e(args):
    """Run E2E tests with Playwright (~2min)."""
    sys.exit(
        run(
            ["uv", "run", "--with", "playwright", "pytest", "tests/e2e/", "-v"]
        ).returncode
    )


def cmd_lint(args):
    """Run ruff lint + format check + ty."""
    lint = run(["uvx", "ruff", "check", "."])
    fmt = run(["uvx", "ruff", "format", "--check", "."])
    types = run(["uvx", "ty", "check"])
    sys.exit(max(lint.returncode, fmt.returncode, types.returncode))


def cmd_validate(args):
    """Full validation: lint + format + typecheck + tests. Fails fast."""
    for cmd in [
        ["uvx", "ruff", "check", "."],
        ["uvx", "ruff", "format", "--check", "."],
        ["uvx", "ty", "check"],
        ["uv", "run", "pytest", "tests/unit/", "merlin-bot/tests/", "-v"],
    ]:
        result = run(cmd)
        if result.returncode != 0:
            sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(
        description="Merlin development task runner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", title="commands")

    sub.add_parser("test", help="Run unit tests (~4s)")
    sub.add_parser("test-e2e", help="Run E2E tests with Playwright (~2min)")
    sub.add_parser("lint", help="Run ruff lint + format check + pyright")
    sub.add_parser(
        "validate", help="Full validation: lint + format + typecheck + tests"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    commands = {
        "test": cmd_test,
        "test-e2e": cmd_test_e2e,
        "lint": cmd_lint,
        "validate": cmd_validate,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
