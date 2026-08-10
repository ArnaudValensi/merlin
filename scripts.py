"""Merlin development task runner.

Usage: uv run scripts.py <command>
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

# Pin the linters/typechecker so `validate` is reproducible. An unpinned
# `uvx ruff` / `uvx ty` resolves to whatever is latest, and newer releases
# change formatting (e.g. Python blocks inside markdown) or tighten type
# inference, breaking validate on unrelated files. Bump these deliberately.
RUFF = "ruff@0.16.2"
TY = "ty@0.0.69"


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
    """Run E2E tests with Playwright (~2min).

    Ensures both Firefox and Chromium are installed first. Firefox covers
    the markdown / project-switcher suites; Chromium with swiftshader covers
    the 3D preview suite (Firefox headless lacks a usable WebGL context on
    most Linux CI images). Playwright's installer is idempotent — already
    present browsers are skipped, so this adds no latency after first run.
    """
    install = run(
        [
            "uv",
            "run",
            "--with",
            "playwright",
            "playwright",
            "install",
            "firefox",
            "chromium",
        ]
    )
    if install.returncode != 0:
        sys.exit(install.returncode)
    sys.exit(
        run(
            ["uv", "run", "--with", "playwright", "pytest", "tests/e2e/", "-v"]
        ).returncode
    )


def cmd_lint(args):
    """Run ruff lint + format check + ty."""
    lint = run(["uvx", RUFF, "check", "."])
    fmt = run(["uvx", RUFF, "format", "--check", "."])
    types = run(["uvx", TY, "check"])
    sys.exit(max(lint.returncode, fmt.returncode, types.returncode))


def cmd_validate(args):
    """Full validation: lint + format + typecheck + tests. Fails fast."""
    for cmd in [
        ["uvx", RUFF, "check", "."],
        ["uvx", RUFF, "format", "--check", "."],
        ["uvx", TY, "check"],
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
